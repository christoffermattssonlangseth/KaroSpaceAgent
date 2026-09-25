"""The envelope grammar that constrains offline decoding, and the token mask a
LocalModel builds from it — both exercised without loading a model."""
import numpy as np
import pytest

from karospace_agent.offline_grammar import EnvelopeGrammar
from karospace_agent.offline_session import LocalModel


TOOLS = ["inspect_input", "inspect_structure", "run_export"]


def test_prefix_of_each_envelope_is_typing():
    g = EnvelopeGrammar(TOOLS)
    assert g.classify("") == "typing"
    assert g.classify("{") == "typing"
    assert g.classify('{"mess') == "typing"
    assert g.classify('{"tool":"insp') == "typing"


def test_completed_skeletons_release_or_complete():
    g = EnvelopeGrammar(TOOLS)
    assert g.classify('{"message":"') == "released"
    assert g.classify('{"message":"Done."}') == "released"
    assert g.classify('{"tool":"run_export","arguments":') == "released"
    assert g.classify('{"tool":"run_export","arguments":{"paths":[]}}') == "released"
    assert g.classify('{"describe_tool":"inspect_input"}') == "complete"


def test_invented_or_malformed_tokens_are_invalid():
    g = EnvelopeGrammar(TOOLS)
    assert g.classify('{"tool":"bash') == "invalid"          # not a registered name
    assert g.classify('{"cmd":"') == "invalid"               # not a registered key
    assert g.classify(" ") == "invalid"                      # must open with '{'
    assert g.classify('{"describe_tool":"inspect_input"} x') == "invalid"


class FakeTokenizer:
    """A minimal byte-per-id tokenizer over a fixed alphabet plus one EOS id."""
    def __init__(self):
        self.pieces = ['{', '}', '"', ':', ',', 'm', 'e', 's', 'a', 'g', 'r',
                       't', 'o', 'l', 'i', 'n', 'p', 'c', 'u', '_', 'x', 'Z', ' ', '<eos>']
        self.eos_token_ids = {len(self.pieces) - 1}

    def get_vocab(self):
        return {p: i for i, p in enumerate(self.pieces)}

    def decode(self, ids):
        return "".join("" if i in self.eos_token_ids else self.pieces[i] for i in ids)


def _model_with(tokenizer):
    model = LocalModel.__new__(LocalModel)   # skip mlx-heavy __init__
    model.tokenizer = tokenizer
    model._id_texts = None
    model._structural_cache = {}
    return model


def test_structural_ids_drop_out_of_charset_and_eos_tokens():
    g = EnvelopeGrammar(TOOLS)
    model = _model_with(FakeTokenizer())
    ids = {i for i, _ in model._structural_ids(g.charset)}
    tok = FakeTokenizer()
    assert tok.pieces.index('Z') not in ids     # 'Z' never appears in the skeleton
    assert tok.pieces.index(' ') not in ids      # space never appears in the skeleton
    assert (len(tok.pieces) - 1) not in ids      # the EOS id is excluded
    assert tok.pieces.index('{') in ids


def test_processor_masks_everything_but_the_valid_next_token():
    mx = pytest.importorskip("mlx.core")
    g = EnvelopeGrammar(TOOLS)
    tok = FakeTokenizer()
    model = _model_with(tok)
    processor = model._envelope_processor(g, prompt_len=0)
    logits = mx.ones((1, len(tok.pieces)))
    # From empty text only '{' can legally start the envelope.
    out = np.array(processor(mx.array([], dtype=mx.int32), logits))[0]
    allowed = {i for i, v in enumerate(out) if v > -1e8}
    assert allowed == {tok.pieces.index('{')}


def test_processor_releases_and_stops_masking_in_free_content():
    mx = pytest.importorskip("mlx.core")
    g = EnvelopeGrammar(TOOLS)
    tok = FakeTokenizer()
    model = _model_with(tok)
    processor = model._envelope_processor(g, prompt_len=0)
    # Tokens spelling `{"message":"` — once released, no token is masked.
    released = [tok.pieces.index(c) for c in '{"message":"']
    logits = mx.ones((1, len(tok.pieces)))
    out = np.array(processor(mx.array(released, dtype=mx.int32), logits))[0]
    assert all(v > -1e8 for v in out)
