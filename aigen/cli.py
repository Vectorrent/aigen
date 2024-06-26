import fire

from .aigen import aigen
from .TokenDataset import TokenDataset
from .tokenizers import train_tokenizer


def aigen_cli(**kwargs):
    """Entrypoint for the CLI"""
    fire.Fire(
        {
            "encode": encode_cli,
            "train": train_cli,
            "generate": generate_cli,
            "train_tokenizer": train_tokenizer_cli,
        }
    )


def encode_cli(file_path: str, **kwargs):
    """Encode + compress a dataset"""
    TokenDataset(file_path, save_cache=True, **kwargs)


def train_cli(file_path: str, **kwargs):
    """Train on a dataset."""
    ai = aigen(**kwargs)

    from_cache = file_path.endswith(".tar.gz")
    dataset = TokenDataset(file_path, from_cache=from_cache, **kwargs)

    ai.train(dataset, **kwargs)


def generate_cli(**kwargs):
    """Generate from a trained model, or download one if not present."""

    ai = aigen(**kwargs)
    ai.generate(**kwargs)


def train_tokenizer_cli(files: str, **kwargs):
    """Trains a tokenizer on the specified file."""
    train_tokenizer(files, **kwargs)
