"""Utility functions for batch processing."""
import logging
import os
from typing import TYPE_CHECKING, Any, Optional, Union

from ray.llm._internal.common.utils.cloud_utils import (
    CloudMirrorConfig,
    is_remote_path,
)
from ray.llm._internal.common.utils.download_utils import CloudModelDownloader

if TYPE_CHECKING:
    from transformers import PreTrainedTokenizer, PreTrainedTokenizerFast

AnyTokenizer = Union["PreTrainedTokenizer", "PreTrainedTokenizerFast", Any]

logger = logging.getLogger(__name__)


def get_cached_tokenizer(tokenizer: AnyTokenizer) -> AnyTokenizer:
    """Get tokenizer with cached properties.
    This will patch the tokenizer object in place.
    By default, transformers will recompute multiple tokenizer properties
    each time they are called, leading to a significant slowdown. This
    function caches these properties for faster access.
    Args:
        tokenizer: The tokenizer object.
    Returns:
        The patched tokenizer object.
    """
    chat_template = getattr(tokenizer, "chat_template", None)
    # For VLM, the text tokenizer is wrapped by a processor.
    if hasattr(tokenizer, "tokenizer"):
        tokenizer = tokenizer.tokenizer
        # Some VLM's tokenizer has chat_template attribute (e.g. Qwen/Qwen2-VL-7B-Instruct),
        # however some other VLM's tokenizer does not have chat_template attribute (e.g.
        # mistral-community/pixtral-12b). Therefore, we cache the processor's chat_template.
        if chat_template is None:
            chat_template = getattr(tokenizer, "chat_template", None)

    tokenizer_all_special_ids = set(tokenizer.all_special_ids)
    tokenizer_all_special_tokens_extended = tokenizer.all_special_tokens_extended
    tokenizer_all_special_tokens = set(tokenizer.all_special_tokens)
    tokenizer_len = len(tokenizer)

    class CachedTokenizer(tokenizer.__class__):  # type: ignore
        @property
        def all_special_ids(self):
            return tokenizer_all_special_ids

        @property
        def all_special_tokens(self):
            return tokenizer_all_special_tokens

        @property
        def all_special_tokens_extended(self):
            return tokenizer_all_special_tokens_extended

        @property
        def chat_template(self):
            return chat_template

        def __len__(self):
            return tokenizer_len

    CachedTokenizer.__name__ = f"Cached{tokenizer.__class__.__name__}"

    tokenizer.__class__ = CachedTokenizer
    return tokenizer


def download_hf_model(model_source: str, tokenizer_only: bool = True) -> str:
    """Download the HF model from the model source.

    Args:
        model_source: The model source path.
        tokenizer_only: Whether to download only the tokenizer.

    Returns:
        The local path to the downloaded model.
    """

    bucket_uri = None
    if is_remote_path(model_source):
        bucket_uri = model_source

    mirror_config = CloudMirrorConfig(bucket_uri=bucket_uri)
    downloader = CloudModelDownloader(model_source, mirror_config)
    return downloader.get_model(tokenizer_only=tokenizer_only)


def download_lora_adapter(
    lora_name: str,
    remote_path: Optional[str] = None,
) -> str:
    """If remote_path is specified, pull the lora to the local
    directory and return the local path.

    Args:
        lora_name: The lora name.
        remote_path: The remote path to the lora. If specified, the remote_path will be
            used as the base path to load the lora.

    Returns:
        The local path to the lora if remote_path is specified, otherwise the lora name.
    """
    assert not is_remote_path(
        lora_name
    ), "lora_name cannot be a remote path (s3:// or gs://)"

    if remote_path is None:
        return lora_name

    lora_path = os.path.join(remote_path, lora_name)
    mirror_config = CloudMirrorConfig(bucket_uri=lora_path)
    downloader = CloudModelDownloader(lora_name, mirror_config)
    return downloader.get_model(tokenizer_only=False)
