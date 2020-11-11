import argparse
import torch
from torch import nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torchvision.datasets import CIFAR10
import torchvision.transforms as T
import numpy as np

import pytorch_lightning as pl
import pytorch_lightning.metrics.functional as FM

from pl_bolts.optimizers import LinearWarmupCosineAnnealingLR
from pl_bolts.datamodules import CIFAR10DataModule, STL10DataModule
from pl_bolts.transforms.dataset_normalizations import (
    cifar10_normalization,
    stl10_normalization,
)

from resnet import (
    resnet18_encoder,
    resnet18_decoder,
    resnet50_encoder,
    resnet50_decoder,
)
from online_eval import SSLOnlineEvaluator
from metrics import gini_score, KurtosisScore
from transforms import Transforms

distributions = {
    "laplace": torch.distributions.Laplace,
    "normal": torch.distributions.Normal,
}

encoders = {"resnet18": resnet18_encoder, "resnet50": resnet50_encoder}
decoders = {"resnet18": resnet18_decoder, "resnet50": resnet50_decoder}


def discretized_logistic(mean, logscale, sample, binsize=1 / 256):
    mean = mean.clamp(min=-0.5 + 1 / 512, max=0.5 - 1 / 512)
    scale = torch.exp(logscale)
    sample = (torch.floor(sample / binsize) * binsize - mean) / scale
    log_pxz = torch.log(
        torch.sigmoid(sample + binsize / scale) - torch.sigmoid(sample) + 1e-7
    )
    return log_pxz.sum(dim=(1, 2, 3))


def gaussian_likelihood(mean, logscale, sample):
    scale = torch.exp(logscale)
    dist = torch.distributions.Normal(mean, scale)
    log_pxz = dist.log_prob(sample)
    return log_pxz.sum(dim=(1, 2, 3))


def kl_divergence_mc(p, q, num_samples=1):
    x = p.rsample([num_samples])
    log_px = p.log_prob(x)
    log_qx = q.log_prob(x)
    # mean over num_samples, sum over z_dim
    return (log_px - log_qx).mean(dim=0).sum(dim=(1))


class Projection(nn.Module):
    def __init__(self, input_dim=2048, hidden_dim=2048, output_dim=128):
        super().__init__()
        self.output_dim = output_dim
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim

        if self.hidden_dim > 0:
            self.model = nn.Sequential(
                nn.Linear(self.input_dim, self.hidden_dim, bias=True),
                nn.BatchNorm1d(self.hidden_dim),
                nn.ReLU(),
                nn.Linear(self.hidden_dim, self.output_dim, bias=False))
        else:
            self.model = nn.Linear(self.input_dim, self.output_dim)

    def forward(self, x):
        return self.model(x)


class VAE(pl.LightningModule):
    def __init__(
        self,
        input_height,
        kl_coeff=0.1,
        latent_dim=256,
        lr=1e-4,
        encoder="resnet18",
        decoder="resnet18",
        prior="normal",
        posterior="normal",
        projection="linear",
        first_conv=False,
        maxpool1=False,
        unlabeled_batch=False,
        max_epochs=100,
        scheduler=False,
    ):
        super(VAE, self).__init__()

        self.save_hyperparameters()
        self.lr = lr
        self.input_height = input_height
        self.in_channels = 3
        self.latent_dim = latent_dim
        self.unlabeled_batch = unlabeled_batch
        self.projection = projection
        self.max_epochs = max_epochs
        self.scheduler = scheduler

        self.encoder = encoders[encoder](first_conv, maxpool1)
        self.decoder = decoders[decoder](
            self.latent_dim, self.input_height, first_conv, maxpool1
        )

        self.log_scale = nn.Parameter(torch.Tensor([0.0]))

        if self.projection == 'linear':
            self.fc_mu = Projection(
                input_dim=self.encoder.out_dim,
                hidden_dim=0,
                output_dim=self.latent_dim
            )
            self.fc_var = Projection(
                input_dim=self.encoder.out_dim,
                hidden_dim=0,
                output_dim=self.latent_dim
            )
        else:
            self.fc_mu = Projection(input_dim=self.encoder.out_dim, output_dim=self.latent_dim)
            self.fc_var = Projection(input_dim=self.encoder.out_dim, output_dim=self.latent_dim)
    
        self.prior = prior
        self.posterior = posterior

        #self.train_kurtosis = KurtosisScore()
        #self.val_kurtosis = KurtosisScore()

    def forward(self, x):
        x = self.encoder(x)
        mu = self.fc_mu(x)
        log_var = self.fc_var(x)
        p, q, z = self.sample(mu, log_var)
        return z, self.decoder(z), p, q

    def sample(self, mu, log_var):
        std = torch.exp(log_var / 2)
        p = distributions[self.prior](torch.zeros_like(mu), torch.ones_like(std))
        q = distributions[self.posterior](mu, std)
        z = q.rsample()
        return p, q, z

    def step(self, batch, batch_idx):
        if self.unlabeled_batch:
            batch = batch[0]

        (x1, x2, _), y = batch

        z, x1_hat, p, q = self.forward(x1)

        log_pxz = discretized_logistic(x1_hat, self.log_scale, x2)
        log_qz = q.log_prob(z)
        log_pz = p.log_prob(z)

        kl = kl_divergence_mc(p, q)

        elbo = (kl - log_pxz).mean()
        bpd = elbo / (
            self.input_height * self.input_height * self.in_channels * np.log(2.0)
        )

        gini = gini_score(z)

        # TODO: this should be epoch metric
        #kurt = kurtosis_score(z)

        n = torch.tensor(x1.size(0)).type_as(x1)
        marg_log_px = torch.logsumexp(log_pxz + log_pz.sum(dim=-1) - log_qz.sum(dim=-1), dim=0) - torch.log(n)

        logs = {
            "kl": kl.mean(),
            "elbo": elbo,
            "gini": gini.mean(),
            #"kurtosis": kurt,
            "bpd": bpd,
            "log_pxz": log_pxz.mean(),
            "marginal_log_px": marg_log_px.mean(),
        }

        return elbo, logs

    def training_step(self, batch, batch_idx):
        loss, logs = self.step(batch, batch_idx)
        self.log_dict({f"train_{k}": v for k, v in logs.items()}, on_step=True, on_epoch=False)

        #self.train_kurtosis.update(z)
        #self.log("train_kurtosis_score", self.train_kurtosis, on_step=False, on_epoch=True)

        return loss

    def validation_step(self, batch, batch_idx):
        loss, logs = self.step(batch, batch_idx)
        self.log_dict({f"val_{k}": v for k, v in logs.items()})

        #self.val_kurtosis.update(z)
        #self.log("val_kurtosis_score", self.val_kurtosis, on_step=False, on_epoch=True)

        return loss

    def configure_optimizers(self):
        optimizer =  torch.optim.Adamax(self.parameters(), lr=self.lr)

        if self.scheduler:
            scheduler = LinearWarmupCosineAnnealingLR(
                optimizer=optimizer,
                warmup_epochs=10,
                max_epochs=self.max_epochs,
                warmup_start_lr=0,
                eta_min=1e-6,
            )

            return [optimizer], [scheduler]
        else:
            return optimizer


if __name__ == "__main__":
    # TODO: model specific args and stuff
    pl.seed_everything(0)

    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=str, default="cifar10", help="stl10/cifar10")
    parser.add_argument("--num_workers", type=int, default=8)
    parser.add_argument("--scheduler", action='store_true')

    parser.add_argument("--latent_dim", type=int, default=256)
    parser.add_argument("--projection", type=str, default='linear', help="linear/non_linear")
    parser.add_argument("--learning_rate", type=float, default=1e-3)
    parser.add_argument("--kl_coeff", type=float, default=0.1)

    parser.add_argument("--flip", action='store_true')
    parser.add_argument("--jitter_strength", type=float, default=1.)

    parser.add_argument("--prior", type=str, default="normal")
    parser.add_argument("--posterior", type=str, default="normal")

    parser.add_argument("--first_conv", action="store_true")
    parser.add_argument("--maxpool1", action="store_true")

    parser.add_argument("--encoder", default="resnet18", choices=encoders.keys())
    parser.add_argument("--decoder", default="resnet18", choices=decoders.keys())

    parser.add_argument("--batch_size", type=int, default=256)

    tf_choices = ["original", "global", "local"]
    parser.add_argument("--input_transform", default="original", choices=tf_choices)
    parser.add_argument("--recon_transform", default="original", choices=tf_choices)

    parser.add_argument("--max_epochs", type=int, default=200)
    parser.add_argument("--gpus", default="1")

    args = parser.parse_args()

    # TODO: clean up these if statements
    if args.dataset == "cifar10":
        dm_cls = CIFAR10DataModule
    elif args.dataset == "stl10":
        dm_cls = STL10DataModule

    dm = dm_cls(
        data_dir="data", batch_size=args.batch_size, num_workers=args.num_workers
    )
    if args.dataset == "stl10":
        dm.train_dataloader = dm.train_dataloader_mixed
        dm.val_dataloader = dm.val_dataloader_mixed

    args.input_height = dm.size()[-1]

    dm.train_transforms = Transforms(
        size=args.input_height,
        input_transform=args.input_transform,
        recon_transform=args.recon_transform,
        normalize_fn=lambda x: x - 0.5,
        flip=args.flip,
        jitter_strength=args.jitter_strength,
    )
    dm.test_transforms = Transforms(
        size=args.input_height, normalize_fn=lambda x: x - 0.5
    )
    dm.val_transforms = Transforms(
        size=args.input_height, normalize_fn=lambda x: x - 0.5
    )

    model = VAE(
        input_height=args.input_height,
        latent_dim=args.latent_dim,
        lr=args.learning_rate,
        kl_coeff=args.kl_coeff,
        prior=args.prior,
        posterior=args.posterior,
        projection=args.projection,
        encoder=args.encoder,
        decoder=args.decoder,
        first_conv=args.first_conv,
        maxpool1=args.maxpool1,
        unlabeled_batch=(args.dataset == "stl10"),
        max_epochs=args.max_epochs,
        scheduler=args.scheduler,
    )

    online_eval = SSLOnlineEvaluator(
        z_dim=model.encoder.out_dim, num_classes=dm.num_classes, drop_p=0.0
    )

    if args.dataset == "stl10":

        def to_device(batch, device):
            (_, _, x), y = batch[1]  # use labelled portion of batch
            x = x.to(device)
            y = y.to(device)
            return x, y

        online_eval.to_device = to_device

    trainer = pl.Trainer(
        gpus=args.gpus, max_epochs=args.max_epochs, callbacks=[online_eval]
    )
    trainer.fit(model, dm)
