import torch
from torch import nn
import torch.nn.functional as F
from torch.nn import Parameter


class BasicCNNDecoder(torch.nn.Module):
    def __init__(self,
                 input_size,
                 latent_size,
                 decoder_kernel_size,
                 decoder_dilations,
                 dropout_ratio):
        super(BasicCNNDecoder, self).__init__()
        self.latent_size = latent_size
        self.input_size = input_size
        self.dropout_ratio = dropout_ratio
        self.decoder_dilations = decoder_dilations

        if isinstance(decoder_kernel_size, int):
            self.decoder_kernel_size = [decoder_kernel_size]
        elif isinstance(decoder_kernel_size, list):
            self.decoder_kernel_size = decoder_kernel_size
        else:
            raise NotImplementedError("Unrecognized hyper parameters: {}".format(decoder_kernel_size))

        self.dropout = nn.Dropout(self.dropout_ratio)
        self.decoder_kernels, self.decoder_biases, self.decoder_paddings = self.module_def()

    def module_def(self):
        assert len(self.decoder_kernel_size) <= 3
        decoder_kernels = []
        for i, out_channel in enumerate(self.decoder_kernel_size):
            if i == 0:
                in_channel = self.latent_size + self.input_size
            else:
                in_channel = self.decoder_kernel_size[i - 1]
            decoder_kernels.append(nn.Parameter(torch.Tensor(out_channel, in_channel, 3).normal_(0, 0.05)))

        decoder_biases = [nn.Parameter(torch.Tensor(out_channel).normal_(0, 0.05))
                          for out_channel in self.decoder_kernel_size]

        decoder_paddings = [self.effective_k(3, self.decoder_dilations[i]) - 1
                            for i in range(len(decoder_kernels))]

        return decoder_kernels, decoder_biases, decoder_paddings

    @staticmethod
    def effective_k(k, d):
        """
        :param k: kernel width
        :param d: dilation size
        :return: effective kernel width when dilation is performed
        """
        return (k - 1) * d + 1

    def forward(self, decoder_input, noise):
        '''
        :param decoder_input: [batch_size, length, embedding_size]
        :param noise: [batch_size, latent_size]
        :return:
        '''
        device = decoder_input.device
        batch_size, seq_len, _ = decoder_input.size()

        z = noise.unsqueeze(1).expand(-1, seq_len, -1)
        decoder_input = torch.cat([decoder_input, z], 2)
        decoder_input = self.dropout(decoder_input)

        # x is tensor with shape [batch_size, input_size=in_channels, seq_len=input_width]
        x = decoder_input.transpose(1, 2).contiguous()

        for layer, kernel in enumerate(self.decoder_kernels):
            # apply conv layer with non-linearity and drop last elements of sequence to perfrom input shifting
            x = F.conv1d(x,
                         weight=kernel.to(device),
                         bias=self.decoder_biases[layer].to(device),
                         dilation=self.decoder_dilations[layer],
                         padding=self.decoder_paddings[layer])
            x_width = x.size(2)
            x = x[:, :, :(x_width - self.decoder_paddings[layer])].contiguous()
            x = F.relu(x)

        result = x.transpose(1, 2).contiguous()
        return result


class HybridDecoder(nn.Module):
    '''
    Code Reference: https://github.com/kefirski/hybrid_rvae
    '''

    def __init__(self, embedding_size, latent_size, hidden_size, num_dec_layers, rnn_type, vocab_size):
        super(HybridDecoder, self).__init__()

        self.vocab_size = vocab_size
        self.hidden_size = hidden_size
        self.latent_size = latent_size
        self.embedding_size = embedding_size
        self.num_dec_layers = num_dec_layers
        self.rnn_type = rnn_type

        self.cnn = nn.Sequential(
            nn.ConvTranspose1d(self.latent_size, 512, 4, 2, 0),
            nn.BatchNorm1d(512),
            nn.ELU(),

            nn.ConvTranspose1d(512, 512, 4, 2, 0, output_padding=1),
            nn.BatchNorm1d(512),
            nn.ELU(),

            nn.ConvTranspose1d(512, 256, 4, 2, 0),
            nn.BatchNorm1d(256),
            nn.ELU(),

            nn.ConvTranspose1d(256, 256, 4, 2, 0, output_padding=1),
            nn.BatchNorm1d(256),
            nn.ELU(),

            nn.ConvTranspose1d(256, 128, 4, 2, 0),
            nn.BatchNorm1d(128),
            nn.ELU(),

            nn.ConvTranspose1d(128, self.vocab_size, 4, 2, 0)
        )

        if rnn_type == 'lstm':
            self.rnn = nn.LSTM(embedding_size + vocab_size, hidden_size, num_dec_layers, batch_first=True)
        elif rnn_type == "gru":
            self.rnn = nn.GRU(embedding_size + vocab_size, hidden_size, num_dec_layers, batch_first=True)
        elif rnn_type == "rnn":
            self.rnn = nn.RNN(embedding_size + vocab_size, hidden_size, num_dec_layers, batch_first=True)
        else:
            raise ValueError("The RNN type in hybrid decoder must in ['lstm', 'gru', 'rnn'].")

        self.token_vocab = nn.Linear(self.hidden_size, self.vocab_size)

    def forward(self, decoder_input, latent_variable):
        """
        :param latent_variable: An float tensor with shape of [batch_size, latent_size]
        :param decoder_input: An float tensot with shape of [batch_size, max_seq_len, embed_size]
        :return: two tensors with shape of [batch_size, max_seq_len, vocab_size]
                    for estimating likelihood for whole model and for auxiliary target respectively
        """
        cnn_logits = self.conv_decoder(latent_variable)
        cnn_logits = cnn_logits[:, :decoder_input.size(1), :].contiguous()  # seq_len
        rnn_logits, _ = self.rnn_decoder(cnn_logits, decoder_input)

        return rnn_logits, cnn_logits

    def conv_decoder(self, latent_variable):
        latent_variable = latent_variable.unsqueeze(2)
        logits = self.cnn(latent_variable).permute(0, 2, 1)
        return logits

    def rnn_decoder(self, cnn_logits, decoder_input, initial_state=None):
        outputs, hidden_states = self.rnn(torch.cat([cnn_logits, decoder_input], 2), initial_state)
        logits = self.token_vocab(outputs)
        return logits, hidden_states
