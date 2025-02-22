"""
Module containing the LSWT model implementations.

Contains:
    - LSWTPreTrainedModel: abstract base class for the LSWT.
    - LSWTModel: backbone base class with no head.
    - LSWTForCausalLM: causal head model containing an LSWTModel instance.
"""

from transformers import PreTrainedModel
from transformers.modeling_outputs import BaseModelOutputWithPast, CausalLMOutputWithPast
import torch

from .configuration import LSWTConfig
from .layers import SharedEmbeddings, RotaryEmbedding, LSWTBlock

class LSWTPreTrainedModel( PreTrainedModel ):
    """
    Base class for LSW Transformer models.

    Class attributes:
        - config_class: The config class to use for this model architecture.
        - base_model_prefix: A string indicating the attribute associated to the base model in derived
        classes of same architecture adding modules on top of the base model.
    """

    config_class = LSWTConfig
    base_model_prefix = 'model'

    def _init_weights( self, module ):
        std = self.config.init_std

        if isinstance( module, torch.nn.Linear ):
            module.weight.data.normal_( mean=0.0, std=std )
            if module.bias is not None:
                module.bias.data.zero_()

        elif isinstance( module, torch.nn.Embedding ):
            module.weight.data.normal_( mean=0.0, std=std )
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()

        elif isinstance( module, torch.nn.LayerNorm ):
            module.bias.data.zero_()
            module.weight.data.fill_( 1.0 )

    def get_param_groups( self ) -> list[dict]:
        """
        Returns optimizer parameter groups with weight decay disabled for certain params.
        Weight decay is disabled for:
            - layer norm
            - bias terms
            - embedding weights

        Returns:
            List[Dict]: list of param groups to be used by the optimizer
        """

        decay_params = []
        non_decay_params = []

        non_decay_names = [ 'norm', 'bias', 'embedding.weight' ]

        for name, p in self.named_parameters():
            if p.requires_grad:
                if any( i in name for i in non_decay_names ):
                    non_decay_params.append( p )

                else:
                    decay_params.append( p )

        return [
            { 'params': decay_params },
            { 'params': non_decay_params, 'weight_decay': 0.0 }
        ]
    
    def trim_cache(
        self,
        cache: list[torch.Tensor],
        trim: int | None = 0,
    ) -> list[torch.Tensor]:
        """ Trims the key and value tuple to a max length.
        
        Should be applied per layer, rather than to the list of all past key values.
        
        Args:
            cache (list[torch.Tensor]): The key value cache to trim.
            trim (int, optional): Desired trim size. Zero means no trim. Defaults to 0.
        
        Returns:
            list[torch.Tensor]: Trimmed cache
        """
        
        if trim is not None:
            return [ kv[ :, :, -trim :, : ] for kv in cache ]
        return cache

    def cache_to(
        self,
        cache: list[list[torch.Tensor]] | None,
        device: str | torch.device,
        trim: int = 0,
        non_blocking: bool = False,
    ) -> list[list[torch.Tensor]] | None:
        """
        Moves KV cache between devices.
        
        TODO: deprecate trim != 0

        Args:
            cache (Optional[list[list[torch.Tensor]]]): Key value cache to move
            device (str | torch.device): the device to move to
            trim (int, optional): Desired trim size. Zero means no trim. Defaults to 0.
            non_blocking (bool): Determines if the transfer should be `non_blocking`. Defaults to False.

        Returns:
            list[list[torch.Tensor]]: Moved key value cache
        """

        if cache is not None:
            cache = [
                [
                    kv.detach()[ :, :, -trim : , : ].to(
                        device=device,
                        non_blocking=non_blocking
                    ) for kv in layer
                ] for layer in cache
            ]
        return cache


class LSWTModel( LSWTPreTrainedModel ):
    """
    Base model class for the LSW Transformer decoder.

    Contains the input embeddings and model backbone, but does not contain the model head.
    """

    def __init__( self, config: LSWTConfig, parent_embeddings: torch.Tensor | None=None ):
        """
        Constructs a new LSWTModel.

        Args:
            config (LSWTConfig): Config for the LSWT architecture
            parent_embeddings (Optional[torch.Tensor], optional): Optinal warm start embeddings.
        """

        super().__init__( config )

        self.input_embedding = SharedEmbeddings( config.vocab_size, config.d_vocab )
        self.input_proj = torch.nn.Linear( config.d_vocab, config.d_model, bias=False )
        self.input_norm = torch.nn.LayerNorm( config.d_model )

        self.rope_embedding = RotaryEmbedding( config )

        self.blocks = torch.nn.ModuleList( [ LSWTBlock( config ) for _ in range( config.n_layers ) ] )

        self.output_norm = torch.nn.LayerNorm( config.d_model )

        self.post_init()

        if parent_embeddings is not None:
            self.input_embedding.embedding.weight = torch.nn.Parameter( torch.clone( parent_embeddings ) )

        if not config.trainable_embeddings:
            self.input_embedding.requires_grad_( False )
            self.input_embedding.half()
        else:
            self.input_embedding.requires_grad_( True )

    def get_input_embeddings( self ):
        return self.input_embedding.embedding

    def embed_input( self, input_ids: torch.LongTensor ) -> torch.Tensor:
        """
        Embedds and projects inputs.

        Args:
            input_ids (torch.LongTensor): input ids of size [Batch x Seq_Length]

        Returns:
            torch.Tensor: input embeddings of size [Batch x Seq_Length x D_Model]
        """
        embeddings = self.input_embedding( input_ids, mode='embed' )
        embeddings = self.input_proj( embeddings )
        return embeddings

    def forward(
        self,
        input_ids: torch.LongTensor | None = None,
        inputs_embeds: torch.Tensor | None = None,
        past_key_values: list[list[torch.Tensor]] | None = None,
        max_key_values: int | None = None,
    ) -> BaseModelOutputWithPast:
        """
        Forward pass function.

        Args:
            input_ids (Optional[torch.LongTensor], optional): input ids of size [Batch x Seq_Length]
            inputs_embeds (Optional[torch.Tensor], optional): input embeddings of size [Batch x Seq_Length x D_Model]
            past_key_values (Optional[List[List[torch.Tensor]]], optional): Previous KV cache for fast decoding or memory.
            max_key_values (int, optional): The max number of past states to keep during generation. Defaults to None.

        Raises:
            ValueError: when both input_ids and inputs_embeds are passed.
            ValueError: when neither input_ids or inputs_embeds are passed.

        Returns:
            BaseModelOutputWithPast: Model outputs
        """

        hidden_state_list = []
        past_key_value_list = []

        if ( input_ids is not None ) and ( inputs_embeds is not None ):
            raise ValueError( 'You cannot specify both input_ids and inputs_embeds at the same time' )
        if ( input_ids is None ) and ( inputs_embeds is None ):
            raise ValueError( 'You have to specify either input_ids or inputs_embeds' )

        # Embed inputs if present
        if input_ids is not None:
            embeddings = self.input_embedding( input_ids, mode='embed' )
            embeddings = self.input_proj( embeddings )
        else:
            embeddings = inputs_embeds
        embeddings = self.input_norm( embeddings )

        hidden_state_list.append( embeddings )

        # RoPE embeddings
        rope_pos, rope_scale = self.rope_embedding( embeddings, past_key_values )

        for i in range( self.config.n_layers ):
            curr_key_values = past_key_values[i] if past_key_values is not None else None
            embeddings, new_key_values = self.blocks[i]( embeddings, curr_key_values, rope_pos, rope_scale )

            hidden_state_list.append( embeddings )
            past_key_value_list.append( self.trim_cache( new_key_values, max_key_values ) )

        # Final normalisation
        embeddings = self.output_norm( embeddings )

        return BaseModelOutputWithPast(
            last_hidden_state=embeddings,
            past_key_values=past_key_value_list, # type: ignore
            hidden_states=hidden_state_list, # type: ignore
            attentions=None,
        )



class LSWTForCausalLM( LSWTPreTrainedModel ):
    """
    Causal LM model class for the LSW Transformer.

    Contains an LSWTModel and a projection layer for the shared embedding LM head.
    """

    def __init__( self, config: LSWTConfig, parent_embeddings: torch.Tensor | None=None ):
        """
        Constructs a new LSWTForCausalLM.

        Args:
            config (LSWTConfig): Config for the LSWT architecture
            parent_embeddings (Optional[torch.Tensor], optional): Optinal warm start embeddings.
        """

        super().__init__( config )

        self.model = LSWTModel( config, parent_embeddings )
        self.head_proj = torch.nn.Linear( config.d_model, config.d_vocab, bias=False )
        self.post_init()

    def get_input_embeddings( self ):
        return self.model.get_input_embeddings()

    def embed_input( self, input_ids: torch.LongTensor ) -> torch.Tensor:
        """
        Embedds and projects inputs.

        Args:
            input_ids (torch.LongTensor): input ids of size [Batch x Seq_Length]

        Returns:
            torch.Tensor: input embeddings of size [Batch x Seq_Length x D_Model]
        """
        return self.model.embed_input( input_ids )

    def forward(
        self,
        input_ids: torch.LongTensor | None = None,
        inputs_embeds: torch.Tensor | None = None,
        past_key_values: list[list[torch.Tensor]] | None = None,

        use_cache=True,
        return_dict=True,
        output_attentions=False,
        output_hidden_states=True,
        
        max_key_values: int | None = None,
    ) -> CausalLMOutputWithPast:
        """
        Forward pass function.

        Args:
            input_ids (Optional[torch.LongTensor], optional): input ids of size [Batch x Seq_Length]
            inputs_embeds (Optional[torch.Tensor], optional): input embeddings of size [Batch x Seq_Length x D_Model]
            past_key_values (Optional[List[List[torch.Tensor]]], optional): Previous KV cache for fast decoding or memory.

            use_cache (bool, optional): If set to `True`, returns KV cache for fast decoding or memory. Defaults to True.
            return_dict (bool, optional): Whether or not to return a CausalLMOutputWithPast. Must be True.
            output_attentions (bool, optional): Returns attentions for all layers. Must be False.
            output_hidden_states (bool, optional): Whether or not to return the hidden states of all layers. Defaults to True.
            
            max_key_values (int, optional): The max number of past states to keep during generation. Defaults to None.

        Returns:
            CausalLMOutputWithPast: Model outputs
        """

        assert return_dict, "Must always return_dict"
        assert not output_attentions, "Must never output_attentions"

        base_outputs = self.model(
            input_ids=input_ids,
            inputs_embeds=inputs_embeds,
            past_key_values=past_key_values,
            max_key_values=max_key_values,
        )

        embeddings = self.head_proj( base_outputs.last_hidden_state )
        logits = self.model.input_embedding( embeddings, mode='linear' )

        return CausalLMOutputWithPast(
            loss=None,
            logits=logits,
            past_key_values=base_outputs.past_key_values if use_cache else None,
            hidden_states=( base_outputs.hidden_states + [ base_outputs.last_hidden_state ] ) if output_hidden_states else None,
            attentions=base_outputs.attentions,
        )

    # TODO: do this legit + remove pylint ignores # pylint: disable=W0511
    def prepare_inputs_for_generation(
        self,
        input_ids,
        past_key_values=None,
        attention_mask=None,
        inputs_embeds=None,
        **kwargs
    ):
        # pylint: disable=W0613
        # pylint: disable=W0612
        if past_key_values is not None:
            past_length = past_key_values[0][0].shape[2]

            # Trim the past key values if a max_key_values model arg is passed            
            max_key_values = kwargs.get( 'max_key_values', 0 ) or 0
            past_key_values = self.cache_to( past_key_values, device=input_ids.device, trim=max_key_values )
            input_ids = input_ids[:, -1 : ]

        # if `inputs_embeds` are passed, we only want to use them in the 1st generation step
        if inputs_embeds is not None and past_key_values is None:
            model_inputs = {"inputs_embeds": inputs_embeds}
        else:
            model_inputs = {"input_ids": input_ids}

        model_inputs.update(
            {
                "past_key_values": past_key_values,
            }
        )
        return model_inputs
