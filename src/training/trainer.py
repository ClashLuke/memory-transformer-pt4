"""
Module containing the training loop components for training LSWTransformer models.
"""

import time
import gc
import os

import tqdm
import numpy as np
from transformers import PreTrainedTokenizerBase
import torch
import torch.distributed as dist
from torch.distributed.optim import ZeroRedundancyOptimizer # type: ignore
from torch.optim import AdamW
from torcheval import metrics
from torcheval.metrics.toolkit import sync_and_compute

from constants import TORCH_COMPILE_OPTIONS, HF_CACHE_DIR
from model.configuration import LSWTConfigTraining
from model.modeling import LSWTForCausalLM

from optimizer.minato import Minato
from optimizer.laprop import LaProp
from .data import PileDataset, OpenOrcaDataset, HFDatasetConfig, HFBatchDataset
from .losses import MLELoss, SimCTGLoss, AccuracyMetric

PILE_PATH_PATTERN = os.environ[ 'PILE_PATH_PATTERN' ]
PILE_SHARDS = int( os.environ[ 'PILE_SHARDS' ] )


class Trainer(): # pylint: disable=R0902
    """ Base class for continuous cached online pre-training.
    """

    def __init__(
        self,
        train_config: LSWTConfigTraining,
        model: LSWTForCausalLM,
        tokenizer: PreTrainedTokenizerBase,
        dataset: str | HFDatasetConfig
    ):
        """ Creates a Trainer instance for the entire pretraining pipeline.
        
        The constructor takes care of the following setup steps:
        - instantiating the optimizer specified in `train_config`
        - loading the pretraining dataset specified in the `dataset` argument
        - instantiating the loss function specified in `train_config`
        - instantiating the accuracy metric
        - setting up FP16 gradient scaling
        - setting up running mean accumulators for loss and accuracy
        - creating the KV cache for CPU offloading

        Args:
            train_config (LSWTConfigTraining): configuration object for the training pipeline.
            model (LSWTForCausalLM): the initialised model
            tokenizer (PreTrainedTokenizerBase): the initialised tokenizer
            dataset (str): dataset for pretraining
        """
        
        self.train_config = train_config
        self.model = model
        self.tokenizer = tokenizer

        self.optimizer = self._load_optimizer()
        self.optimizer_scaler = torch.cuda.amp.GradScaler() # type: ignore

        self.batch_groups = train_config.batch_size // train_config.batch_size_step

        self.past_key_values_list = self._load_cache()
        self.data_loader_train = self._load_dataset( dataset )
        self.loss_function = self._load_loss_function()

        self.acc_function = AccuracyMetric( model.config.vocab_size, model.config.pad_token_id )

        self.metrics = {
            'loss': metrics.Mean().to( 'cuda' ),
            'acc': metrics.Mean().to( 'cuda' ),
        }

        self.optimizer_step = 0


    """ ========================================================================
        Internal Utility functions
        ======================================================================== """

    def _bar_format( self, iter_n, iter_total, elapsed, epoch, loss, acc ) -> str:
        return tqdm.tqdm.format_meter(
            n=iter_n,
            total=iter_total,
            elapsed=elapsed,
            ncols=80,
            unit='it',
            bar_format='{desc}: {percentage:.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}, {rate_fmt}{postfix}]',
            postfix=f'loss={loss:.3f}, acc={acc:.3f}',
            prefix=f'Epoch {epoch}',
        )
    
    def _load_cache( self ):
        batch_groups = self.train_config.batch_size // self.train_config.batch_size_step
        return [ None for _ in range( batch_groups ) ]

    def _load_optimizer( self ) -> torch.optim.Optimizer:        
        if self.train_config.optimizer == 'Minato':
            return Minato(
                params=self.model.get_param_groups(),
                lr=0.0,
                betas=( self.train_config.opt_beta_1, self.train_config.opt_beta_2 ),
                eps=self.train_config.opt_eps,
                rho=self.train_config.opt_rho,
                weight_decay=( self.train_config.opt_weight_decay )
            )

        if self.train_config.optimizer == 'AdamW':
            return AdamW(
                params=self.model.get_param_groups(),
                lr=0.0,
                betas=( self.train_config.opt_beta_1, self.train_config.opt_beta_2 ),
                eps=self.train_config.opt_eps,
                weight_decay=( self.train_config.opt_weight_decay ),
                fused=True,
            )
        
        if self.train_config.optimizer == 'LaProp':
            return LaProp(
                params=self.model.get_param_groups(),
                lr=0.0,
                betas=( self.train_config.opt_beta_1, self.train_config.opt_beta_2 ),
                eps=self.train_config.opt_eps,
                weight_decay=( self.train_config.opt_weight_decay ),
            )

        raise ValueError( 'Invalid optimizer' )

    def _load_loss_function( self ) -> torch.nn.Module:
        if self.train_config.loss_objective == 'MLE':
            return MLELoss(
                self.model.config.vocab_size,
                self.model.config.pad_token_id
            )

        if self.train_config.loss_objective == 'SimCTG':
            return SimCTGLoss(
                self.train_config.loss_sim_margin,
                self.model.config.vocab_size,
                self.model.config.pad_token_id
            )

        raise ValueError( 'Invalid loss function' )
    
    def _load_dataset( self, dataset ):
        if isinstance( dataset, HFDatasetConfig ):
            return HFBatchDataset(
                tokenizer=self.tokenizer,
                seq_length=self.train_config.length_sequence,
                batch_size=self.train_config.batch_size,
                dataset_config=dataset,
                num_proc=self.batch_groups,
            )

        if dataset == 'pile':
            return PileDataset(
                tokenizer=self.tokenizer,
                seq_length=self.train_config.length_sequence,
                batch_size=self.train_config.batch_size,
                dir_pattern=PILE_PATH_PATTERN,
                pile_shards=list( range( PILE_SHARDS ) )
            ).as_data_loader()
        
        if dataset == 'openorca':
            return OpenOrcaDataset(
                tokenizer=self.tokenizer,
                seq_length=self.train_config.length_sequence,
                batch_size=self.train_config.batch_size,
                cache_dir=HF_CACHE_DIR,
            ).as_data_loader()
        
        raise ValueError( 'Invalid dataset choice' )


    """ ========================================================================
        Utility functions
        ======================================================================== """

    def reset_metrics( self ) -> dict[ str, float ]:
        """ Computes the end of epoch metrics and resets them for the next epoch.

        Returns:
            dict[ str, float ]: dictionary of { metric_name : computed_mean }
        """
        
        stats = {}
        for name, metric in self.metrics.items():
            stats[name] = float( metric.compute() )
            metric.reset()
        return stats

    def get_schedule( self ) -> float:
        """ Returns the learning rate percentage based on the Chinchilla schedule.
        
        Note, you must manually scale by the max learning rate.

        Returns:
            float: LR ratio in range [0.0, 1.0]
        """
        
        warmup_ratio = min( self.optimizer_step / self.train_config.lr_warmup_steps, 1.0 )

        tokens_seen = self.optimizer_step * self.train_config.batch_size * self.train_config.length_sequence
        warmup_tokens = self.train_config.lr_warmup_steps * self.train_config.batch_size * self.train_config.length_sequence

        cooldown_ratio = min( max( tokens_seen - warmup_tokens, 0.0 ) / ( self.train_config.lr_cooldown_tokens - warmup_tokens ), 1.0 )
        cooldown_ratio = np.cos( cooldown_ratio * np.pi ) * 0.5 + 0.5
        cooldown_ratio = cooldown_ratio * ( 1.0 - self.train_config.lr_cooldown_ratio ) + self.train_config.lr_cooldown_ratio

        return min( warmup_ratio, cooldown_ratio )

    def get_total_epochs( self ) -> int:
        """ Compute the total number of epochs based on the number of total tokens.
        
        Returns:
            int: total epochs
        """
        
        tokens_per_epoch = self.train_config.batch_size * self.train_config.length_sequence * self.train_config.batches_per_epoch
        return int( np.ceil( self.train_config.lr_cooldown_tokens / tokens_per_epoch ) )


    """ ========================================================================
        Forward Pass
        ======================================================================== """

    def forward_pass( self, tokens, past_key_values, cache_length ):

        torch._inductor.cudagraph_mark_step_begin() # type: ignore # pylint: disable=W0212

        outputs = self.model(
            input_ids=tokens,
            past_key_values=past_key_values,
            use_cache=cache_length > 0,
            max_key_values=cache_length,
        )

        past_key_values = outputs.past_key_values
        logits = outputs.logits
        last_hidden_state = outputs.hidden_states[-1]

        del outputs

        return logits, past_key_values, last_hidden_state


    """ ========================================================================
        Training Functions
        ======================================================================== """

    @torch.compile( **TORCH_COMPILE_OPTIONS )
    def train_sub_step( self, tokens_x, tokens_y, past_key_values ):
        with torch.autocast( device_type='cuda', dtype=torch.float16 ): # type: ignore
            logits, past_key_values, hidden_states = self.forward_pass( tokens_x, past_key_values, self.train_config.length_cache )

            y_pred = logits
            y_true = tokens_y

            mle_loss, aux_loss = self.loss_function( hidden_states, y_pred, tokens_x, y_true )
            accu_loss = ( mle_loss + aux_loss ) / self.batch_groups
        self.optimizer_scaler.scale( accu_loss ).backward() # type: ignore

        logits.detach()
        hidden_states.detach()
        accu_loss.detach()
        y_pred.detach()
        aux_loss.detach()
        accu_loss.detach()

        accuracy = self.acc_function( logits, y_true ).detach()

        return mle_loss.detach(), accuracy, past_key_values

    def train_optim_step( self ):
        self.optimizer_step += 1

        for p_group in self.optimizer.param_groups:
            p_group[ 'lr' ] = self.get_schedule() * self.train_config.lr_max

        if self.train_config.opt_max_grad_norm > 0.0:
            self.optimizer_scaler.unscale_( self.optimizer )
            torch.nn.utils.clip_grad_norm_( self.model.parameters(), self.train_config.opt_max_grad_norm ) # type: ignore

        self.optimizer_scaler.step( self.optimizer )
        self.optimizer_scaler.update()
        self.optimizer.zero_grad()

    def train_batch_step( self, batch ):
        self.model.train()

        tokens_xs, tokens_ys = batch

        tokens_xs = torch.split( tokens_xs.to( device='cuda', non_blocking=True ), self.train_config.batch_size_step )
        tokens_ys = torch.split( tokens_ys.to( device='cuda', non_blocking=True ), self.train_config.batch_size_step )

        for idx in range( self.batch_groups ):
            past_key_values = self.model.cache_to( self.past_key_values_list[idx], 'cuda', non_blocking=True )
            self.past_key_values_list[idx] = None # type: ignore

            loss, accuracy, past_key_values = self.train_sub_step( tokens_xs[idx], tokens_ys[idx], past_key_values )

            past_key_values = self.model.cache_to( past_key_values, 'cpu', non_blocking=True )
            self.past_key_values_list[idx] = past_key_values # type: ignore

            self.metrics[ 'loss' ].update( loss )
            self.metrics[ 'acc' ].update( accuracy )

        self.train_optim_step()

    def train_epoch( self, iterator, epoch ):
        start_time = time.time()

        for batch in range( self.train_config.batches_per_epoch ):
            self.train_batch_step( next( iterator ) )

            bar = self._bar_format(
                iter_n=batch + 1,
                iter_total=self.train_config.batches_per_epoch,
                elapsed=time.time() - start_time,
                epoch=epoch,
                loss=self.metrics[ 'loss' ].compute(),
                acc=self.metrics[ 'acc' ].compute(),
            )

            print( '\r' + bar, end='', flush=True )
        print()

        torch.cuda.empty_cache()
        gc.collect()

        return self.reset_metrics()

class TrainerDDP( Trainer ):
    """ Distributed training class for continuous cached online pre-training.
    """
    
    def __init__(
        self,
        train_config: LSWTConfigTraining,
        model: LSWTForCausalLM,
        tokenizer: PreTrainedTokenizerBase,
        dataset: str,
        ddp_rank: int,
        ddp_world_size: int,
        dataset_shards: int = 30,
    ):
        """ Creates a Distributed Trainer instance for the entire pretraining pipeline.
        
        The constructor takes care of the following setup steps:
        - instantiating the optimizer specified in `train_config` with the Zero policy
        - loading the pretraining dataset specified in the `dataset` argument with DDP sharding
        - instantiating the loss function specified in `train_config`
        - instantiating the accuracy metric
        - setting up FP16 gradient scaling
        - setting up running mean accumulators for loss and accuracy
        - creating the KV cache for CPU offloading

        Args:
            train_config (LSWTConfigTraining): configuration object for the training pipeline.
            model (LSWTForCausalLM): the initialised model
            tokenizer (PreTrainedTokenizerBase): the initialised tokenizer
            dataset (str): dataset for pretraining
            ddp_rank (int): the rank of the current process
            ddp_world_size (int): the total number of processes in the group
            dataset_shards (int): the number of dataset shards, defaults to 30, but 24 recommended
        """
        
        self.ddp_rank = ddp_rank
        self.ddp_world_size = ddp_world_size
        self.dataset_shards = dataset_shards
        
        super().__init__( train_config, model, tokenizer, dataset )

        # Modify batch_groups for DDP
        self.batch_groups = train_config.batch_size // ( train_config.batch_size_step * self.ddp_world_size )
    
    
    """ ========================================================================
        Overridden Internal Utility functions
        ======================================================================== """
    
    def _load_optimizer( self ) -> torch.optim.Optimizer:       
        if self.train_config.optimizer == 'LaProp':
            return ZeroRedundancyOptimizer(
                params=self.model.get_param_groups(),
                optimizer_class=LaProp,
                lr=0.0,
                betas=( self.train_config.opt_beta_1, self.train_config.opt_beta_2 ),
                eps=self.train_config.opt_eps,
                weight_decay=( self.train_config.opt_weight_decay ),
            )
        
        if self.train_config.optimizer == 'Minato':
            return ZeroRedundancyOptimizer(
                params=self.model.get_param_groups(),
                optimizer_class=Minato,
                lr=0.0,
                betas=( self.train_config.opt_beta_1, self.train_config.opt_beta_2 ),
                rho=self.train_config.opt_rho,
                eps=self.train_config.opt_eps,
                weight_decay=( self.train_config.opt_weight_decay ),
            )

        raise ValueError( 'Invalid optimizer' )
    
    def _load_cache( self ):
        batch_groups = self.train_config.batch_size // ( self.train_config.batch_size_step * self.ddp_world_size )
        return [ None for _ in range( batch_groups ) ]
    
    def _load_dataset( self, dataset ):
        if dataset == 'pile':
            return PileDataset(
                tokenizer=self.tokenizer,
                seq_length=self.train_config.length_sequence,
                batch_size=self.train_config.batch_size // self.ddp_world_size,
                dir_pattern=PILE_PATH_PATTERN,
                pile_shards=list( range( self.ddp_rank, self.dataset_shards, self.ddp_world_size ) )
            ).as_data_loader()
        
        raise ValueError( 'Invalid dataset choice' )


    """ ========================================================================
        Overridden Utility functions
        ======================================================================== """
    
    def reset_metrics( self ) -> dict[ str, float ]:
        stats = {}
        for name, metric in self.metrics.items():
            # Syncronise and compute metrics from all devices
            stats[name] = float( sync_and_compute( metric ) )
            
            # Ensure all devices wait for barrier before resetting
            dist.barrier()
            metric.reset()
        return stats

    
    """ ========================================================================
        Overridden Training Functions
        ======================================================================== """
    
    def train_batch_step( self, batch ):
        self.model.train()

        tokens_xs, tokens_ys = batch

        tokens_xs = torch.split( tokens_xs.to( device='cuda', non_blocking=True ), self.train_config.batch_size_step )
        tokens_ys = torch.split( tokens_ys.to( device='cuda', non_blocking=True ), self.train_config.batch_size_step )

        for idx in range( self.batch_groups ):
            self.model.require_backward_grad_sync = ( idx == self.batch_groups - 1 ) # type: ignore
            
            past_key_values = self.model.cache_to( self.past_key_values_list[idx], 'cuda', non_blocking=True )
            self.past_key_values_list[idx] = None # type: ignore
            
            loss, accuracy, past_key_values = self.train_sub_step( tokens_xs[idx], tokens_ys[idx], past_key_values )
            
            past_key_values = self.model.cache_to( past_key_values, 'cpu', non_blocking=True )
            self.past_key_values_list[idx] = past_key_values # type: ignore

            self.metrics[ 'loss' ].update( loss )
            self.metrics[ 'acc' ].update( accuracy )

        self.train_optim_step()
                
        if self.optimizer_step <= 3:
            torch.cuda.empty_cache()
    
    def train_epoch( self, iterator, epoch ):
        # dist.barrier()
        
        start_time = time.time()

        for batch in range( self.train_config.batches_per_epoch ):
            self.train_batch_step( next( iterator ) )

            bar = self._bar_format(
                iter_n=batch + 1,
                iter_total=self.train_config.batches_per_epoch,
                elapsed=time.time() - start_time,
                epoch=epoch,
                loss=float( sync_and_compute( self.metrics[ 'loss' ] ) ),
                acc=float( sync_and_compute( self.metrics[ 'acc' ] ) ),
            )

            if self.ddp_rank == 0:
                print( '\r' + bar, end='', flush=True )
        if self.ddp_rank == 0:
            print()

        torch.cuda.empty_cache()
        gc.collect()

        return self.reset_metrics()