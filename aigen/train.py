import logging
import os
import random
import shutil
import subprocess
import sys

import psutil
import torch
import torchmetrics
from lightning.pytorch import LightningModule
from lightning.pytorch.accelerators import TPUAccelerator
from lightning.pytorch.callbacks import ProgressBar
from lightning.pytorch.strategies import DeepSpeedStrategy
from torch.utils.data import DataLoader
from tqdm.auto import tqdm

from .utils import colors

logging.getLogger("transformers").setLevel(logging.ERROR)


class AIGTrainer(LightningModule):
    """
    A training module for aigen.
    """

    def __init__(self, model, optimizer, scheduler, train_len, hparams, tokenizer):
        super(AIGTrainer, self).__init__()

        self.model, self.optimizer, self.scheduler, self.train_len, self.tokenizer = (
            model,
            optimizer,
            scheduler,
            train_len,
            tokenizer,
        )
        self.automatic_optimization = True
        self.save_hyperparameters(hparams)

    def forward(self, inputs):
        outputs = self.model(**inputs)
        # labels = inputs.get("labels")
        # logits = outputs.get("logits")
        return outputs

    def training_step(self, batch, batch_idx):
        losses = []

        for i, sample in enumerate(batch):
            outputs = self({"input_ids": sample, "labels": sample})
            losses.append(outputs[0])

        loss = sum(losses) / len(losses)

        schedule = self.lr_schedulers()
        schedule.step()

        step = self.global_step
        if hasattr(schedule, "current_step"):
            step = schedule.current_step

        if self.logger:
            self.logger.experiment.add_scalars(
                "vtx",
                {"lr": float(self.trainer.optimizers[0].param_groups[0]["lr"])},
                step,
            )

        return loss

    def validation_step(self, batch, batch_idx):
        outputs = self({"input_ids": batch, "labels": batch})
        loss = outputs[0]
        perplexity = torch.exp(loss)

        schedule = self.lr_schedulers()
        step = self.global_step
        if hasattr(schedule, "current_step"):
            step = schedule.current_step

        if self.logger:
            self.logger.experiment.add_scalars(
                "vtx",
                {"val_loss": float(loss), "val_ppl": float(perplexity)},
                step,
            )

        return loss

    def on_train_epoch_end(self):
        pass

    def configure_optimizers(self):
        "Prepare optimizer"

        return [self.optimizer], [self.scheduler]


class AIGProgressBar(ProgressBar):
    """A variant progress bar that works off of steps and prints periodically."""

    def __init__(
        self,
        save_every,
        generate_every,
        output_dir,
        n_generate,
        gpu,
        train_transformers_only,
        num_layers_freeze,
        petals,
        generation_config,
        target_batch_size,
    ):
        super().__init__()
        self.enabled = True
        self.save_every = save_every
        self.generate_every = generate_every
        self.output_dir = output_dir
        self.n_generate = n_generate
        self.gpu = gpu
        self.steps = 0
        self.prev_avg_loss = None
        self.smoothing = 0.01
        self.train_transformers_only = train_transformers_only
        self.num_layers_freeze = num_layers_freeze
        self.petals = petals
        self.generation_config = generation_config
        self.target_batch_size = target_batch_size

    @property
    def save_every_check(self):
        return self.save_every > 0 and self.steps % self.save_every == 0

    def enabled(self):
        self.enabled = True

    def disable(self):
        self.enabled = False

    def on_train_start(self, trainer, lm):
        super().on_train_start(trainer, lm)
        self.pbar = tqdm(
            total=trainer.max_steps * trainer.accumulate_grad_batches,
            disable=not self.enabled,
            smoothing=0,
            leave=True,
            dynamic_ncols=True,
            file=sys.stdout,
        )
        self.freeze_layers(lm)

    def on_train_end(self, trainer, lm):
        self.pbar.close()
        self.unfreeze_layers(lm)

    def on_train_batch_end(self, trainer, lm, outputs, batch, batch_idx):
        super().on_train_batch_end(trainer, lm, outputs, batch, batch_idx)

        current_loss = float(outputs["loss"])
        current_epoch = trainer.current_epoch
        if lm.train_len > 0:
            current_epoch += batch_idx / lm.train_len
        self.steps += 1
        avg_loss = 0
        if current_loss == current_loss:  # don't add if current_loss is NaN
            avg_loss = self.average_loss(
                current_loss, self.prev_avg_loss, self.smoothing
            )
            self.prev_avg_loss = avg_loss

        if TPUAccelerator.is_available() and self.save_every_check:
            did_unfreeze = False
            if self.enabled:
                self.unfreeze_layers(lm)
                did_unfreeze = True
            self.save_pytorch_model(trainer, lm, tpu=True)
            if did_unfreeze:
                self.freeze_layers(lm)

        if self.enabled:
            did_unfreeze = False
            if not TPUAccelerator.is_available() and self.save_every_check:
                self.unfreeze_layers(lm)
                self.save_pytorch_model(trainer, lm)
                did_unfreeze = True

            if self.generate_every > 0 and self.steps % self.generate_every == 0:
                self.unfreeze_layers(lm)
                self.generate_sample_text(trainer, lm)
                did_unfreeze = True

            if did_unfreeze:
                self.freeze_layers(lm)

        schedule = lm.lr_schedulers()
        step = lm.global_step

        if hasattr(schedule, "current_step"):
            step = schedule.current_step

        if lm.logger:
            lm.logger.experiment.add_scalars(
                "vtx",
                {
                    "train_loss": current_loss,
                    "epoch": current_epoch,
                },
                step,
            )

        color = colors.GREEN
        if current_loss < avg_loss:
            color = colors.BLUE
        elif current_loss > avg_loss:
            color = colors.RED

        bearing = "{:.5f}".format(
            abs(round((current_loss / avg_loss) if avg_loss != 0 else 0, 5))
        )

        c_sym = "+" if current_loss >= 0 else ""
        a_sym = "+" if avg_loss >= 0 else ""

        mem_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf(
            "SC_PHYS_PAGES"
        )  # e.g. 4015976448
        mem_gib = mem_bytes / (1024.0**3)  # e.g. 3.74

        memory = psutil.virtual_memory()

        echo = f"{colors.GREEN}{c_sym}{current_loss:.3f}{colors.WHITE} => Loss => {color}{a_sym}{avg_loss:.3f}{colors.WHITE} => Bearing => {colors.BLUE}{bearing}{random.randint(0,2)}00{colors.WHITE} => System => {colors.BLUE}{memory.percent}%{colors.WHITE}"

        if self.gpu:
            # via pytorch-lightning's get_gpu_memory_map()
            result = subprocess.run(
                [
                    shutil.which("nvidia-smi"),
                    "--query-gpu=memory.used",
                    "--format=csv,nounits,noheader",
                ],
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            gpu_memory = result.stdout.strip().split(os.linesep)
            gpus = f"MB{colors.WHITE} => {colors.BLUE}".join(gpu_memory)
            epoch_string = "{:.3f}".format(current_epoch)
            echo += f" => GPU => {colors.BLUE}{gpus}MB{colors.WHITE} => Epoch => {colors.BLUE}{epoch_string}{colors.WHITE}"

        if hasattr(trainer.strategy, "num_peers"):
            num_peers = trainer.strategy.num_peers
            echo = echo + f" => Peers => {colors.BLUE}{num_peers}{colors.WHITE}"

        if (
            hasattr(schedule, "current_step")
            and step != 0
            and self.pbar.n % self.target_batch_size == 0
        ):
            self.pbar.reset(total=trainer.max_steps)
            for _ in range(step * self.target_batch_size):
                self.pbar.update(1)

        self.pbar.update(1)
        self.pbar.set_description(echo)

    def generate_sample_text(self, trainer, lm):
        lm.model.eval()

        if hasattr(lm.model, "training"):
            lm.model.training = False

        outputs = lm.model.generate(
            inputs=None,
            generation_config=self.generation_config,
            do_sample=True,
            max_new_tokens=222,
            bos_token_id=lm.tokenizer.bos_token_id,
            eos_token_id=lm.tokenizer.eos_token_id,
            pad_token_id=lm.tokenizer.pad_token_id,
        )

        if hasattr(lm.model, "training"):
            lm.model.training = True

        gen_texts = lm.tokenizer.batch_decode(outputs, skip_special_tokens=True)

        lm.model.train()

        for text in gen_texts:
            self.pbar.write(text)
            self.pbar.write(f"={colors.BLUE}=>{colors.WHITE}")

    def save_pytorch_model(self, trainer, lm, tpu=False):
        if self.petals:
            with open(os.path.join(self.output_dir, "prompts.pt"), "wb") as f:
                torch.save(
                    (
                        lm.model.transformer.prompt_embeddings,
                        lm.model.transformer.intermediate_prompt_embeddings,
                    ),
                    f,
                )
        elif tpu:
            import torch_xla.core.xla_model as xm

            lm.model.save_pretrained(
                self.output_dir, save_function=xm.save, safe_serialization=True
            )
        else:
            lm.model.save_pretrained(self.output_dir, safe_serialization=True)

    def average_loss(self, current_loss, prev_avg_loss, smoothing):
        if prev_avg_loss is None:
            return current_loss
        else:
            return (smoothing * current_loss) + (1 - smoothing) * prev_avg_loss

    def modify_layers(self, lm, unfreeze):
        if self.train_transformers_only:
            for name, param in lm.model.named_parameters():
                if self.num_layers_freeze:
                    layer_num = int(name.split(".")[2]) if ".h." in name else None
                    to_freeze = layer_num and layer_num < self.num_layers_freeze
                else:
                    to_freeze = False
                if name == "transformer.wte.weight" or to_freeze:
                    param.requires_grad = unfreeze

    def freeze_layers(self, lm):
        self.modify_layers(lm, False)

    def unfreeze_layers(self, lm):
        self.modify_layers(lm, True)
