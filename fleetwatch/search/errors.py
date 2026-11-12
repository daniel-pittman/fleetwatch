"""Errors the search library raises instead of exiting.

Each message is written for the person running the command: a CLI entry point prints
str(error) and exits 1, which is what the engine's own sys.exit(message) calls did, so the
text and the exit code stay the same for anyone reading the output.
"""


class SearchError(Exception):
    """Base class. str(e) is the complete, user-facing message."""


class ConfigError(SearchError):
    """The index's search_config.yaml names something unusable."""


class EmptyCorpus(SearchError):
    """A build found no chunks under the configured roots."""


class IndexMissing(SearchError):
    """No SQLite index exists at the configured index_path."""


class IndexStale(SearchError):
    """The index was built by a version of the engine with another schema."""


class ModelMismatch(SearchError):
    """The index's vectors came from a different embedding model than the config names."""


class EmbedderUnavailable(SearchError):
    """Nothing can produce sentence embeddings, and the config does not choose TF-IDF."""


class EmbeddingFailed(SearchError):
    """The embedding interpreter ran and failed."""


class ProbesMissing(SearchError):
    """eval was asked for an index with no probes.yaml."""


class NoProbesYet(ProbesMissing):
    """probes.yaml exists but lists no probes, as a new index's template does."""


class ProbesInvalid(SearchError):
    """probes.yaml has a probe whose expect block names nothing the engine can check."""
