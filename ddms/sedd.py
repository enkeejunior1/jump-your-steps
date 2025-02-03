# This loss function code is heavily based on SEDD implementation.
# ref: https://github.com/louaaron/Score-Entropy-Discrete-Diffusion

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from einops import einsum
from typing import Union


class Loss(nn.Module):
    def __init__(self, scheduler):
        super().__init__()
        self.scheduler = scheduler

    def forward(self, log_score, sigma_bar, xt, x0, reduction="sum"):
        """
        TODO: need to verify the code
        """
        # sigma = self.scheduler.sigma[t].unsqueeze(1).expand_as(xt)
        expm1_sigma_bar = torch.where(
            sigma_bar < 0.5,
            torch.expm1(sigma_bar),
            torch.exp(sigma_bar) - 1
        )

        perturbed_pos = xt == self.scheduler.num_vocabs - 1
        ratio = 1 / expm1_sigma_bar[:, None].expand_as(xt)[perturbed_pos] # p(y|x0) / p(xt|x0) = exp(-sigma) / (1 - exp(-sigma))
        y = x0[perturbed_pos]

        neg = ratio * torch.gather(log_score[perturbed_pos], -1, y[..., None]).squeeze(-1)
        pos = log_score[perturbed_pos][:, :-1].exp().sum(dim=-1) # pos = torch.gather(log_score[perturbed_pos].exp(), -1, y[..., None]).squeeze()
        const = ratio * (ratio.log() - 1) # there are no constant term in algorithm 1

        loss = torch.zeros(*xt.shape, device=xt.device)
        loss[perturbed_pos] += (pos - neg + const) # DWDSE loss (but simple loss, do not use sigma scale, i.e., sigma[perturbed_pos] * (pos - neg + const)

        if reduction == "mean":
            return loss.mean()
        if reduction == "sum":
            return loss.sum()
    

class Scheduler(nn.Module):
    """
    We only care about masked diffusion models

    Train 
        1. t, samples -> sigma (alphas_comprod)  - (sample_transition) -> noisy_samples
        2. pred_score = model(samples, t)
        3. score = get_score(samples, noisy_samples)
        4. loss_weight = get_loss_weight(t)
        5. loss = loss_weight * comp_loss(pred_score, score)
        
    Sampling
    """
    def __init__(
        self, args
    ):  
        super().__init__()

        # basic configs
        self.num_vocabs = args.num_vocabs + 1 # "absorb"
        self.length = args.length
        self.eps = args.eps
        self.model_name = args.model_name
        
        # init noise schedule (similar to alphas_cumprod)
        if args.noise_schedule == "loglinear":
            self.sigma_bar = lambda t: -torch.log1p(-(1 - self.eps) * t) # page 15
            self.sigma = lambda t: (1 - self.eps) / (1 - (1 - self.eps) * t) # sigma_bar / dt
        
    def add_noise(
        self, samples: torch.LongTensor, t: Union[int, torch.LongTensor], generator=None, 
    ):
        '''x0 -> xt'''
        # snr
        sigma_bar = self.sigma_bar(t)
        
        # perturb samples (absorb)
        perturb_prob = 1 - (-sigma_bar).exp()
        perturbed_samples = torch.where(
            torch.rand(*samples.shape, device=samples.device, generator=generator) < perturb_prob[:, None],
            self.num_vocabs - 1, samples
        )
        return perturbed_samples
    
    def add_noise_backward_compatible(
        self, samples: torch.LongTensor, t: Union[int, torch.LongTensor], generator=None, 
    ):
        # x0 -> xt
        sigma_bar = self.sigma_bar(t)
        perturb_prob = 1 - (-sigma_bar).exp()
        perturbed_samples = torch.where(
            torch.rand(*samples.shape, device=samples.device, generator=generator) < perturb_prob[:, None],
            self.num_vocabs - 1, samples
        )
        x0 = F.one_hot(samples).float()
        xt = F.one_hot(perturbed_samples).float()

        # create backward compatible xt
        th = perturb_prob[:, None, None]
        th_rot_matrix = einsum(th + (xt - th).detach(), x0, 'b l i, b l j -> b l i j')
        xt_one_hot = einsum(th_rot_matrix, x0, 'b l i j, b l j -> b l i')
        return perturbed_samples, xt_one_hot
    
    def output_to_score(self, output, t=None):
        if self.model_name == 'sedd':
            score = output.exp()
        elif self.model_name == 'd3pm':
            pass
        elif self.model_name == 'maskgit':
            sigma_bar = self.sigma_bar(t)
            perturb_prob = 1 - (-sigma_bar).exp()
            score = (1 - perturb_prob) / perturb_prob * output # https://arxiv.org/abs/2407.21243, eq 3
        elif self.model_name == 'ctmc':
            pass
        else:
            raise ValueError(f'invalid model_name: {self.model_name}')
        return score
    
    def sample_latent(self, num_samples):
        return (self.num_vocabs-1) * torch.ones(num_samples, self.length).long()

    def step(self, output, xt, t, step_size):
        pass


class SchedulerOutput:
    def __init__(self, xt, xt_prob=None, rev_rate=None, tau=None):
        self.xt = xt
        self.tau = tau
        self.xt_prob = xt_prob
        self.rev_rate = rev_rate


class EulerScheduler(Scheduler):
    def Q_tok(self, i):
        '''Q_tok = Q[i, :] (Eq.16 from SEDD paper)'''
        edge = -F.one_hot(i, num_classes=self.num_vocabs)
        edge[i == self.num_vocabs - 1] += 1
        return edge
    
    def Q_tilde(self, xt, score):
        normalized_rate = self.Q_tok(xt) * score
        # ref: https://github.com/louaaron/Score-Entropy-Discrete-Diffusion/blob/main/graph_lib.py
        # to ensure that maintain the rate matrix property (sum_j R_ij = 0)
        normalized_rate.scatter_(-1, xt[..., None], torch.zeros_like(normalized_rate))
        normalized_rate.scatter_(-1, xt[..., None], -normalized_rate.sum(dim=-1, keepdim=True))
        return normalized_rate

    def step(self, output, xt, t, step_size, rev_rate=None, generator=None, if_last=False):
        if rev_rate is None:
            sigma = self.sigma(t)
            score = self.output_to_score(output)
            rev_rate = sigma[..., None, None] * self.Q_tilde(xt, score)
        identity = F.one_hot(xt, num_classes=self.num_vocabs).to(rev_rate)
        xt_prob = identity + step_size * rev_rate
        xt_prob = xt_prob[..., :-1] if if_last else xt_prob
        xt = sample_categorical(xt_prob, generator=generator)
        return SchedulerOutput(xt, xt_prob=xt_prob, rev_rate=rev_rate)


class PCScheduler(Scheduler):
    def Q_tok(self, i):
        '''Q_tok = Q[i, :] (Eq.16 from SEDD paper)'''
        edge = -F.one_hot(i, num_classes=self.num_vocabs)
        edge[i == self.num_vocabs - 1] += 1
        return edge
    
    def Q_tilde(self, xt, score):
        normalized_rate = self.Q_tok(xt) * score
        # ref: https://github.com/louaaron/Score-Entropy-Discrete-Diffusion/blob/main/graph_lib.py
        # to ensure that maintain the rate matrix property (sum_j R_ij = 0)
        normalized_rate.scatter_(-1, xt[..., None], torch.zeros_like(normalized_rate))
        normalized_rate.scatter_(-1, xt[..., None], -normalized_rate.sum(dim=-1, keepdim=True))
        return normalized_rate

    def step(self, output, xt, t, step_size, rev_rate=None, generator=None, if_last=False, is_corrector=False):
        if rev_rate is None:
            sigma = self.sigma(t)
            score = self.output_to_score(output)
            rev_rate = sigma[..., None, None] * self.Q_tilde(xt, score)
        if is_corrector:
            rev_rate += sigma[..., None, None] * self.Q_tok(xt)
        identity = F.one_hot(xt, num_classes=self.num_vocabs).to(rev_rate)
        xt_prob = identity + step_size * rev_rate
        xt_prob = xt_prob[..., :-1] if if_last else xt_prob
        xt = sample_categorical(xt_prob, generator=generator)
        return SchedulerOutput(xt, xt_prob=xt_prob, rev_rate=rev_rate)
    

class GillespieScheduler(EulerScheduler):
    def add_noise(
        self, samples: torch.FloatTensor, k: Union[int, torch.LongTensor], generator=None, 
    ):
        # 1. token prob
        token_prob = torch.rand(*samples.shape, device=samples.device, generator=generator)
        if samples.shape[1] - k != 0:
            values,idx = torch.topk(token_prob, samples.shape[1] - k, dim=-1, largest=False)
            t = (values.max(dim=-1).values / (1-self.eps)).clamp(min=0, max=1)
            perturbed_samples = torch.scatter(samples, -1, idx, self.num_vocabs - 1)
        else:
            t = torch.zeros(samples.shape[0], device=samples.device) + 1e-5
            perturbed_samples = samples
        return perturbed_samples, t
    
    def step(self, output, xt, t, dk, rev_rate=None, generator=None, if_last=False):
        '''Algorithm 1 from https://arxiv.org/abs/2407.21243'''
        if rev_rate is None:
            sigma = self.sigma(t)
            score = self.output_to_score(output)
            rev_rate = sigma[..., None, None] * self.Q_tilde(xt, score)

        # sample holding time
        r = rev_rate[..., :-1]
        tau = sample_exponential(r.sum(dim=-1), generator=generator)

        # sample token 
        tau, idx = torch.topk(tau, dk, dim=-1, largest=False)
        r = torch.gather(r, 1, idx[..., None].repeat(1,1,r.size(-1)))
        r = r / r.sum(dim=-1, keepdim=True)
        xt = torch.scatter(xt, -1, idx, sample_categorical(r, generator=generator))
        return SchedulerOutput(xt, rev_rate=rev_rate, tau=tau.max(dim=-1).values)
    

class AnalyticScheduler(Scheduler):
    def staggered_score(self, score, delta_sigma_bar):
        '''
        TODO need to understand
        ref: https://github.com/louaaron/Score-Entropy-Discrete-Diffusion/blob/0605786da5ccb5747545e26d66fdf477187598b6/graph_lib.py#L234
        '''
        extra_const = (1 - (delta_sigma_bar[:, None]).exp()) * score.sum(dim=-1)
        score *= delta_sigma_bar[:, None, None].exp()
        score[..., -1] += extra_const
        return score
    
    def transp_transition(self, i, sigma):
        '''
        TODO need to understand
        ref: https://github.com/louaaron/Score-Entropy-Discrete-Diffusion/blob/0605786da5ccb5747545e26d66fdf477187598b6/graph_lib.py#L218
        '''
        sigma = unsqueeze_as(sigma, i[..., None])
        edge = (-sigma).exp() * F.one_hot(i, num_classes=self.num_vocabs)
        edge += torch.where(
            i == self.num_vocabs - 1,
            1 - (-sigma).squeeze(-1).exp(),
            0
        )[..., None]
        return edge
    
    def step(self, output, xt, t, step_size, generator=None, if_last=False, **kwargs):
        curr_sigma_bar = self.sigma_bar(t)
        next_sigma_bar = self.sigma_bar(t - step_size)
        delta_sigma_bar = curr_sigma_bar - next_sigma_bar
        score = self.output_to_score(output)

        stag_score = self.staggered_score(score, delta_sigma_bar)
        probs = stag_score * self.transp_transition(xt, delta_sigma_bar)
        probs = probs[..., :-1] if if_last else probs
        xt = sample_categorical(probs, generator=generator)
        return SchedulerOutput(xt)
    
    
def sample_exponential(lambda_, eps=1e-6, generator=None):
    if generator is None:
        exp_noise = torch.rand_like(lambda_)
    else:
        exp_noise = torch.rand(lambda_.shape, generator=generator, device=generator.device).to(lambda_)
    return -1 / (lambda_ + eps) * torch.log(eps + (1 - eps) * exp_noise)

def sample_categorical(categorical_probs, eps=1e-6, generator=None):
    '''use gumbel-max trick, but given probability'''
    if generator is None:
        gumbel_noise = torch.rand_like(categorical_probs)
    else:
        gumbel_noise = torch.rand(categorical_probs.shape, generator=generator, device=generator.device).to(categorical_probs)
    gumbel_noise = (eps - torch.log(eps + (1 - eps) * gumbel_noise))
    return torch.argmax(categorical_probs / gumbel_noise, dim=-1)

    
# def sample_categorical(categorical_probs, method="hard"):
#     if method == "hard":
#         gumbel_norm = 1e-10 - (torch.rand_like(categorical_probs) + 1e-10).log()
#         return (categorical_probs / gumbel_norm).argmax(dim=-1)
#     else:
#         raise ValueError(f"Method {method} for sampling categorical variables is not valid.")
    
    
def unsqueeze_as(x, y, back=True):
    if back:
        return x.view(*x.shape, *((1,) * (len(y.shape) - len(x.shape))))
    else:
        return x.view(*((1,) * (len(y.shape) - len(x.shape))), *x.shape)