''' Define the sublayers in encoder/decoder layer '''

import torch
import torch.nn as nn
import torch.nn.init as init
from .Modules import BottleLinear as Linear
from .Modules import ScaledDotProductAttention
# from transformer.Modules import BottleLayerNormalization as LayerNormalization
from .Modules import LayerNormalization


class MultiHeadAttention(nn.Module):
    ''' Multi-Head Attention module '''

    def __init__(self, n_head, d_model, d_k, d_v, position_dpa=None, dropout=0.1, scaled_dropout=0.1,
                 use_batch_norm=True, residual_bool=False
                 ):

        super(MultiHeadAttention, self).__init__()

        self.n_head = n_head
        self.d_k = d_k
        self.d_v = d_v

        self.use_batch_norm = use_batch_norm
        self.residual_bool = residual_bool

        # TODO: default without cuda, do we need cuda call here?
        self.w_qs = nn.Parameter(torch.FloatTensor(n_head, d_model, d_k).cuda())
        self.w_ks = nn.Parameter(torch.FloatTensor(n_head, d_model, d_k).cuda())
        self.w_vs = nn.Parameter(torch.FloatTensor(n_head, d_model, d_v).cuda())

        # for dpa, fill with ones
        self.dpa_qs = nn.Parameter(torch.FloatTensor(n_head, d_model, d_k).cuda())
        init.constant(self.dpa_qs, 1)

        # TODO: test this, initially dropout was always set to 0.1!
        # TODO: higher makes the model stable, but Recall is now much lower!
        self.attention = ScaledDotProductAttention(d_model, scaled_dropout)

        if self.use_batch_norm:  # batch norm
            self.layer_norm = nn.BatchNorm1d(d_model)
        else:  # layer norm
            self.layer_norm = LayerNormalization(d_model)

        self.proj = Linear(n_head*d_v, d_model)

        self.dropout = nn.Dropout(dropout)

        # TODO: experiment with he and xavier
        init.kaiming_normal(self.w_qs)  # xavier_normal used originally
        init.kaiming_normal(self.w_ks)  # xavier_normal
        init.kaiming_normal(self.w_vs)  # xavier_normal

    def forward(self, q, k, v, attn_mask=None, position_dpa=None):

        d_k, d_v = self.d_k, self.d_v
        n_head = self.n_head

        residual = q

        verbose_sizes = False

        mb_size, len_q, d_model = q.size()
        mb_size, len_k, d_model = k.size()
        mb_size, len_v, d_model = v.size()

        if position_dpa is not None and verbose_sizes:
            print()
            print("q before repeat:", q.size())

        # treat as a (n_head) size batch
        q_s = q.repeat(n_head, 1, 1).view(n_head, -1, d_model)  # n_head x (mb_size*len_q) x d_model
        k_s = k.repeat(n_head, 1, 1).view(n_head, -1, d_model)  # n_head x (mb_size*len_k) x d_model
        v_s = v.repeat(n_head, 1, 1).view(n_head, -1, d_model)  # n_head x (mb_size*len_v) x d_model

        if position_dpa is not None and verbose_sizes:
            print("q_s after repeat:", q_s.size())

        # treat the result as a (n_head * mb_size) size batch
        q_s = torch.bmm(q_s, self.w_qs).view(-1, len_q, d_k)    # (n_head*mb_size) x len_q x d_k
        k_s = torch.bmm(k_s, self.w_ks).view(-1, len_k, d_k)    # (n_head*mb_size) x len_k x d_k
        v_s = torch.bmm(v_s, self.w_vs).view(-1, len_v, d_v)    # (n_head*mb_size) x len_v x d_v

        if position_dpa is not None and verbose_sizes:
            print("q_s after bmm:", q_s.size())
            print()

        # TODO: set the same size to dpa as to the seq_input size
        if position_dpa is not None:

            verbose_sizes = True

            if verbose_sizes:
                print("dpa before repeat:", position_dpa.size())

            # size before this: [50, 91, 360]
            # size after: [3, 4550, 360]
            position_dpa = position_dpa.repeat(n_head, 1, 1).view(n_head, -1, d_model)

            if verbose_sizes:
                print("dpa after repeat:", position_dpa.size())

            # ????
            # TODO: this fails if we don't resize by multiplying, first column is tripled for some reason
            # self.dpa_qs is a matrix of ones filled out in init

            # size after multiplying: [3, 4550, 120]             # n_head x (batch_size*len_q) x d_model
            # size after view: [150, 91, 120]                    # (n_head*batch_size) x len_q x d_k
            position_dpa = torch.bmm(position_dpa, self.dpa_qs)  # n_head x (batch_size*len_q) x d_model
            position_dpa = position_dpa.view(-1, len_q, d_k)     # (n_head*batch_size) x len_q x d_k

            # this view doesn't work
            # position_dpa = position_dpa.view(n_head, d_model, d_k).view(-1, len_q, d_k)

            if verbose_sizes:
                print("dpa after bmm:", position_dpa.size())
                print()

        # perform attention, result size = (n_head * mb_size) x len_q x d_v
        if attn_mask is not None:

            if position_dpa is not None:

                # print("using diagonal positional encodings 1")

                # TODO: the size of dpa changes before this! investigate!
                # print("q_s before scaled_attn:", q_s.size())
                # print("dpa before scaled_attn:", position_dpa.size())

                outputs, attns = self.attention(
                    q_s, k_s, v_s,
                    attn_mask=attn_mask.repeat(n_head, 1, 1),
                    position_dpa=position_dpa
                )

            else:
                outputs, attns = self.attention(q_s, k_s, v_s, attn_mask=attn_mask.repeat(n_head, 1, 1))

        # don't use masking if none given
        else:
            outputs, attns = self.attention(q_s, k_s, v_s)

        # back to original mb_size batch, result size = mb_size x len_q x (n_head*d_v)
        outputs = torch.cat(torch.split(outputs, mb_size, dim=0), dim=-1)

        # project back to residual size
        outputs = self.proj(outputs)
        outputs = self.dropout(outputs)

        if self.use_batch_norm:  # use batch norm
            # batch_norm expects (batch_size, h_units, seq_len), we have (batch_s, seq_len, h_units)
            outputs = outputs.permute(0, 2, 1)

            # have to make everything contiguous to make it run on CUDA
            if self.residual_bool:  # if new residual, add it only in PFF later
                outputs = self.layer_norm(outputs.contiguous())
            else:  # use typical self-attention implementation
                # TODO: make sure this actually works as it should
                outputs = self.layer_norm(outputs.contiguous() + residual.permute(0, 2, 1).contiguous())

            # move columns back
            return outputs.permute(0, 2, 1), attns

        else:  # use layer norm
            if self.residual_bool:  # if new residual, add it only in PFF later
                return self.layer_norm(outputs), attns
            else:
                return self.layer_norm(outputs + residual), attns


class PositionwiseFeedForward(nn.Module):
    ''' A two-feed-forward-layer module '''

    def __init__(self, d_hid, d_inner_hid, dropout=0.1):
        super(PositionwiseFeedForward, self).__init__()

        self.use_batch_norm = True

        self.w_1 = nn.Conv1d(d_hid, d_inner_hid, 1)  # position-wise
        self.w_2 = nn.Conv1d(d_inner_hid, d_hid, 1)  # position-wise

        if self.use_batch_norm:
            self.layer_norm = nn.BatchNorm1d(d_hid)
        else:
            self.layer_norm = LayerNormalization(d_hid)

        self.dropout = nn.Dropout(dropout)

        # instead of relu also tried: ELU,LeakyReLU, PReLU,ReLU6,RReLU,SELU
        self.relu = nn.RReLU()  # nn.ReLU() used originally

    def forward(self, x, residual=None):

        # redirect the residual from the MultiHeadAttention directly to the end of FFN if given one
        if residual is None:
            residual = x

        w1_output = self.relu(self.w_1(x.transpose(1, 2)))
        output = self.w_2(w1_output).transpose(2, 1)
        output = self.dropout(output)

        if self.use_batch_norm:
            # batch_norm expects (batch_size, h_units, seq_len), we have (batch_s, seq_len, h_units)
            outputs = output.permute(0, 2, 1)
            residual_permuted = residual.permute(0, 2, 1)
            # have to make everything contiguous to make it run on CUDA
            outputs = self.layer_norm(outputs.contiguous()+residual_permuted.contiguous())
            # move columns back
            return outputs.permute(0, 2, 1)
        else:
            return self.layer_norm(output + residual)
