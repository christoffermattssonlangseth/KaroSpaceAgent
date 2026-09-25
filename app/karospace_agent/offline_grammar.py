"""A tiny envelope grammar for constrained decoding in the offline loop.

Small local models fail at this workflow mostly by emitting malformed JSON,
the wrong envelope, or an invented tool name. Rather than validate after the
fact and burn a retry, we constrain generation so the output can only ever be a
valid prefix of one allowed envelope:

    {"message":"..."}                       a conversational answer
    {"describe_tool":"<registered name>"}   ask for a tool's schema
    {"tool":"<registered name>","arguments":<json>}   call a tool

Everything here is pure Python and tokenizer-free so it can be unit-tested
without loading a model; LocalModel turns a grammar into an mlx logits processor.

The classification is deliberately a prefix machine, not a full JSON parser:

- "typing"    the text so far is a strict prefix of the fixed skeleton (up to and
              including the tool name); the next token must keep it on that path,
              so a misspelled key or invented tool name is impossible.
- "released"  the skeleton is complete and free content follows (a message body
              or an arguments object); we stop constraining and let the existing
              json/jsonschema check catch a malformed tail.
- "complete"  a describe_tool envelope is fully formed; only end-of-sequence
              should follow.
- "invalid"   the text cannot become any allowed envelope.
"""
from __future__ import annotations


class EnvelopeGrammar:
    def __init__(self, tool_names):
        names = sorted(tool_names)
        # Compact form, matching the few-shot examples (no spaces after colons),
        # so the model is guided along the exact shape it has already seen.
        self.release_guides = ['{"message":"'] + [f'{{"tool":"{n}","arguments":' for n in names]
        self.complete_guides = [f'{{"describe_tool":"{n}"}}' for n in names]
        self._prefix_guides = self.release_guides + self.complete_guides
        # Characters that can ever appear in the constrained skeleton. Tokens with
        # any other character can never extend it, so LocalModel skips them.
        self.charset = frozenset("".join(self._prefix_guides))

    def classify(self, text):
        for guide in self.release_guides:
            if text.startswith(guide):
                return "released"
        if text in self.complete_guides:
            return "complete"
        for guide in self._prefix_guides:
            if guide.startswith(text):
                return "typing"
        return "invalid"
