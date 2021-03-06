# -*- coding: utf-8 -*- #
"""*********************************************************************************************"""
#   FileName     [ model_dual.py ]
#   Synopsis     [ Implementation of the VQ Layer, GST Layer, and dual transformer models ]
#   Author       [ Andy T. Liu (Andi611) ]
#   Copyright    [ Copyleft(c), Speech Lab, NTU, Taiwan ]
"""*********************************************************************************************"""


###############
# IMPORTATION #
###############
import copy
import torch
import torch.nn as nn
import torch.nn.init as init
import torch.nn.functional as F
from transformer.model import TransformerConfig, TransformerInitModel
from transformer.model import TransformerSpecPredictionHead, TransformerModel
from transformer.model_quantize import VectorQuantizeLayer_GB, VectorQuantizeLayer_L2
from transformer.model_quantize import GlobalStyleTokenLayer, LinearLayer


class DualTransformerConfig(TransformerConfig):
    """Configuration class to store the configuration of a `VqTransformerModel`.
    """
    def __init__(self, config):
        super(DualTransformerConfig, self).__init__(config)
        
        self.dual_transformer = config['transformer']['dual_transformer']
        self.decoder = config['dual_transformer']['decoder']
        self.intermediate_pe = config['dual_transformer']['intermediate_pe']
        self.combine = config['dual_transformer']['combine']
        self.phone_type = config['dual_transformer']['phone_type']
        self.phone_size = config['dual_transformer']['phone_size']
        self.phone_dim = config['dual_transformer']['phone_dim']
        self.speaker_type = config['dual_transformer']['speaker_type']
        self.speaker_size = config['dual_transformer']['speaker_size']
        self.speaker_dim = config['dual_transformer']['speaker_dim']
        self.average_pooling = config['dual_transformer']['average_pooling']
        self.pre_train = config['dual_transformer']['pre_train']


####################
# DUAL TRANSFORMER #
####################
class DualTransformerForMaskedAcousticModel(TransformerInitModel):
    """Dual Transformer model with the masked acoustic modeling head.
    This module comprises the Dual Transformer model followed by the masked acoustic modeling head.

    Params:
        `config`: a TransformerConfig class instance with the configuration to build a new model
        `intput_dim`: int,  input dimension of model
        `output_dim`: int,  output dimension of model
        `output_attentions`: If True, also output attentions weights computed by the model at each layer. Default: False
        `keep_multihead_output`: If True, saves output of the multi-head attention module with its gradient.
            This can be used to compute head importance metrics. Default: False

    Inputs:
        `phonetic_input`: a torch.LongTensor of shape [batch_size, sequence_length, feature_dimension]
            input to the phonetic encoder
            with the selected frames processed as masked frames during training,
            generated by the `process_train_MAM_data()` function in `transformer/mam.py`.
        `speaker_input`: a torch.LongTensor of shape [batch_size, sequence_length, feature_dimension]
            input to the speaker encoder
            with the selected frames processed as masked frames during training,
            generated by the `process_train_MAM_data()` function in `transformer/mam.py`.
        `pos_enc`: a torch.LongTensor of shape [batch_size, sequence_length, hidden_size],
            generated by the `fast_position_encoding()` function in `transformer/mam.py`.
        `masked_label`: masked acoustic modeling labels - torch.LongTensor of shape [batch_size, sequence_length]
            with indices selected in [1, 0]. All labels set to -1 are ignored, the loss
            is only computed for the labels set to 1.
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `spce_label`: a torch.LongTensor of shape [batch_size, sequence_length, feature_dimension]
            which are the ground truth spectrogram used as reconstruction labels.
        `head_mask`: an optional torch.Tensor of shape [num_heads] or [num_layers, num_heads] with indices between 0 and 1.
            It's a mask to be used to nullify some heads of the transformer. 1.0 => head is fully masked, 0.0 => head is not masked.

    Outputs:
        if `spec_label` and `mask_label` is not `None`:
            Outputs the masked acoustic modeling loss and predicted spectrogram.
        if `spec_label` and `mask_label` is `None`:
            Outputs the masked acoustic modeling predicted spectrogram of shape [batch_size, sequence_length, output_dim * downsample_rate].

    Example usage:
    ```python
    spec_input = torch.LongTensor(spec_frames)
    pos_enc = torch.LongTensor(position_encoding(seq_len=len(spec_frames)))

    config = TransformerConfig(config)

    model = TransformerForMaskedLM(config)
    masked_spec_logits = model(spec_input, pos_enc)
    ```
    """
    def __init__(self, config, input_dim, output_dim, output_attentions=False, keep_multihead_output=False):
        super(DualTransformerForMaskedAcousticModel, self).__init__(config, output_attentions)
        
        assert config.dual_transformer, 'This config attribute should be set to True!'
        self.decoder = config.decoder
        self.use_pe = config.intermediate_pe
        self.combine = config.combine
        self.phone_dim = config.phone_dim
        self.speaker_dim = config.speaker_dim
        self.average_pooling = config.average_pooling

        # build encoder
        if self.phone_dim != 0: self.PhoneticTransformer = TransformerPhoneticEncoder(config, input_dim, output_attentions, keep_multihead_output)
        if self.speaker_dim != 0: self.SpeakerTransformer = TransformerSpeakerEncoder(config, input_dim, output_attentions, keep_multihead_output)

        if self.phone_dim == 0 and self.speaker_dim == 0:
            raise ValueError
        elif self.phone_dim == 0 or self.speaker_dim == 0:
            code_dim = max(self.phone_dim, self.speaker_dim)
        elif config.combine == 'concat':
            code_dim = self.PhoneticTransformer.out_dim + self.SpeakerTransformer.out_dim
        elif config.combine == 'add':
            assert self.PhoneticTransformer.out_dim == self.SpeakerTransformer.out_dim
            code_dim = self.PhoneticTransformer.out_dim
        else:
            raise NotImplementedError
        
        # build decoder
        if self.decoder:
            if self.use_pe: self.SPE = nn.Parameter(torch.FloatTensor([1.0])) # Scaled positional encoding (SPE) introduced in https://arxiv.org/abs/1809.08895
            self.SpecTransformer = TransformerModel(config, input_dim=code_dim,
                                                    output_attentions=output_attentions,
                                                    keep_multihead_output=keep_multihead_output,
                                                    with_input_module=True if self.use_pe else False)
        self.SpecHead = TransformerSpecPredictionHead(config, output_dim if output_dim is not None else input_dim, code_dim if not self.decoder else None)

        # weight handling
        if len(config.pre_train) > 0:
            if len(config.pre_train) == 1: config.pre_train = [config.pre_train]
            all_states1 = torch.load(config.pre_train[0], map_location='cpu')
            all_states2 = torch.load(config.pre_train[-1], map_location='cpu')
            if self.phone_dim != 0: self.PhoneticTransformer.Transformer = load_model(self.PhoneticTransformer.Transformer, all_states1['Transformer'])
            if self.speaker_dim != 0: self.SpeakerTransformer.Transformer = load_model(self.SpeakerTransformer.Transformer, all_states2['Transformer'])
        self.apply(self.init_Transformer_weights)
        self.loss = nn.L1Loss() 

    def forward(self, phonetic_input, speaker_input, pos_enc, mask_label=None, attention_mask=None, spec_label=None, head_mask=None):
        # dual encoder forward
        if self.phone_dim != 0: 
            phonetic_outputs = self.PhoneticTransformer(phonetic_input, pos_enc, attention_mask, head_mask=head_mask)
        else: 
            phonetic_outputs = (None, None) if self.output_attentions else None
        if self.speaker_dim != 0: 
            speaker_outputs = self.SpeakerTransformer(speaker_input, pos_enc, attention_mask, head_mask=head_mask)
        else: 
            speaker_outputs = (None, None) if self.output_attentions else None
        
        if self.output_attentions: 
            phonetic_attentions, phonetic_code = phonetic_outputs
            speaker_attentions, speaker_code = speaker_outputs
            all_attentions = [phonetic_attentions, speaker_attentions]
        else:
            phonetic_code = phonetic_outputs
            speaker_code = speaker_outputs
        
        # replicate code
        if self.speaker_dim != 0 and self.average_pooling: 
            speaker_code = speaker_code.repeat(1, phonetic_code.size(1), 1)
        
        # combine code 
        if self.speaker_dim == 0:
            code = phonetic_code
        elif self.phone_dim == 0:
            code = speaker_code
        elif self.combine == 'concat':
            code = torch.cat((phonetic_code, speaker_code), dim=2)
        elif self.combine == 'add':
            code = phonetic_code + speaker_code
        else:
            raise NotImplementedError

        # decoder forward
        if self.decoder:
            outputs = self.SpecTransformer(code, (self.SPE * pos_enc) if self.use_pe else None,
                                        attention_mask=attention_mask,
                                        output_all_encoded_layers=False,
                                        head_mask=head_mask)
            if self.output_attentions:
                decoder_attentions, sequence_output = outputs
                all_attentions.append(decoder_attentions)
            else:
                sequence_output = outputs
        else:
            sequence_output = code
        pred_spec, pred_state = self.SpecHead(sequence_output)

        # compute objective
        if spec_label is not None and mask_label is not None:
            assert mask_label.sum() > 0, 'Without any masking, loss might go NaN. Modify your data preprocessing (utility/mam.py)'
            masked_spec_loss = self.loss(pred_spec.masked_select(mask_label), spec_label.masked_select(mask_label))
            return masked_spec_loss, pred_spec
        elif self.output_attentions:
            return all_attentions, pred_spec
        return pred_spec, pred_state


####################
# PHONETIC ENCODER #
####################
class TransformerPhoneticEncoder(TransformerInitModel):
    '''
    spec_input --- [batch_size, sequence_length, feature_dimension]
    sequence_output --- [batch_size, sequence_length, phone_dim]
    '''
    def __init__(self, config, input_dim, output_attentions=False, keep_multihead_output=False, with_recognizer=True):
        super(TransformerPhoneticEncoder, self).__init__(config, output_attentions)
        self.Transformer = TransformerModel(config, input_dim, output_attentions=output_attentions,
                                            keep_multihead_output=keep_multihead_output)
        if config.phone_type == 'l2' and with_recognizer:
            self.PhoneRecognizer = VectorQuantizeLayer_L2(config.hidden_size, config.phone_size, config.phone_dim)
        elif config.phone_type == 'gb' and with_recognizer:
            self.PhoneRecognizer = VectorQuantizeLayer_GB(config.hidden_size, config.phone_size, config.phone_dim)
        elif config.phone_type == 'gst' and with_recognizer:
            self.PhoneRecognizer = GlobalStyleTokenLayer(config.hidden_size, config.phone_size, config.phone_dim)
        elif config.phone_type == 'linear' and with_recognizer:
            self.PhoneRecognizer = LinearLayer(config.hidden_size, config.phone_dim)
        elif config.phone_type == 'none' and with_recognizer:
            with_recognizer = False
        elif with_recognizer:
            raise NotImplementedError
        
        self.with_recognizer = with_recognizer
        self.apply(self.init_Transformer_weights)
        self.out_dim = self.PhoneRecognizer.out_dim if with_recognizer else config.hidden_size

    def forward(self, spec_input, pos_enc, attention_mask=None, output_all_encoded_layers=False, head_mask=None):
        outputs = self.Transformer(spec_input, pos_enc, attention_mask,
                                   output_all_encoded_layers=output_all_encoded_layers,
                                   head_mask=head_mask)
        
        if self.output_attentions:
            all_attentions, sequence_output = outputs
        else:
            sequence_output = outputs

        if self.with_recognizer:
            phonetic_code = self.PhoneRecognizer(sequence_output)
        else:
            phonetic_code = sequence_output
        
        if self.output_attentions:
            return all_attentions, phonetic_code
        return phonetic_code


###################
# SPEAKER ENCODER #
###################
class TransformerSpeakerEncoder(TransformerInitModel):
    '''
    spec_input --- [batch_size, sequence_length, feature_dimension]
    sequence_output --- [batch_size, 1, speaker_dim] or [batch_size, sequence_length, speaker_dim]
    '''
    def __init__(self, config, input_dim, output_attentions=False, keep_multihead_output=False, with_recognizer=True):
        super(TransformerSpeakerEncoder, self).__init__(config, output_attentions)
        self.Transformer = TransformerModel(config, input_dim, output_attentions=output_attentions,
                                            keep_multihead_output=keep_multihead_output)
        if config.speaker_type == 'gst' and with_recognizer:
            self.SpeakerRecognizer = GlobalStyleTokenLayer(config.hidden_size, config.speaker_size, config.speaker_dim)
        elif config.speaker_type == 'linear' and with_recognizer:
            self.SpeakerRecognizer = LinearLayer(config.hidden_size, config.speaker_dim)
        elif config.phone_type == 'none' and with_recognizer:
            with_recognizer = False
        elif with_recognizer:
            raise NotImplementedError

        self.average_pooling = config.average_pooling
        self.with_recognizer = with_recognizer
        self.apply(self.init_Transformer_weights)
        self.out_dim = self.SpeakerRecognizer.out_dim if with_recognizer else config.hidden_size

    def forward(self, spec_input, pos_enc, attention_mask=None, output_all_encoded_layers=False, head_mask=None):
        outputs = self.Transformer(spec_input, pos_enc, attention_mask,
                            output_all_encoded_layers=output_all_encoded_layers,
                            head_mask=head_mask)
        
        if self.output_attentions:
            all_attentions, sequence_output = outputs
        else:
            sequence_output = outputs
        if self.average_pooling:
            sequence_output = sequence_output.mean(dim=1).unsqueeze(1) # (batch_size, 1, speaker_dim)

        if self.with_recognizer:
            speaker_code = self.SpeakerRecognizer(sequence_output)
        else:
            speaker_code = sequence_output

        if self.output_attentions:
            return all_attentions, speaker_code
        return speaker_code


##############
# LOAD MODEL #
##############
def load_model(transformer_model, state_dict):
        try:
            old_keys = []
            new_keys = []
            for key in state_dict.keys():
                new_key = None
                if 'gamma' in key:
                    new_key = key.replace('gamma', 'weight')
                if 'beta' in key:
                    new_key = key.replace('beta', 'bias')
                if new_key:
                    old_keys.append(key)
                    new_keys.append(new_key)
            for old_key, new_key in zip(old_keys, new_keys):
                state_dict[new_key] = state_dict.pop(old_key)

            missing_keys = []
            unexpected_keys = []
            error_msgs = []
            # copy state_dict so _load_from_state_dict can modify it
            metadata = getattr(state_dict, '_metadata', None)
            state_dict = state_dict.copy()
            if metadata is not None:
                state_dict._metadata = metadata

            def load(module, prefix=''):
                local_metadata = {} if metadata is None else metadata.get(prefix[:-1], {})
                module._load_from_state_dict(
                    state_dict, prefix, local_metadata, True, missing_keys, unexpected_keys, error_msgs)
                for name, child in module._modules.items():
                    if child is not None:
                        load(child, prefix + name + '.')

            load(transformer_model)
            if len(missing_keys) > 0:
                print('Weights of {} not initialized from pretrained model: {}'.format(
                    transformer_model.__class__.__name__, missing_keys))
            if len(unexpected_keys) > 0:
                print('Weights from pretrained model not used in {}: {}'.format(
                    transformer_model.__class__.__name__, unexpected_keys))
            if len(error_msgs) > 0:
                raise RuntimeError('Error(s) in loading state_dict for {}:\n\t{}'.format(
                                    transformer_model.__class__.__name__, '\n\t'.join(error_msgs)))
            print('[Transformer] - Pre-trained weights loaded!')
            return transformer_model

        except: 
            raise RuntimeError('[Transformer] - Pre-trained weights NOT loaded!')
