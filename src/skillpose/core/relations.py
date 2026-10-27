"""Step 3b: how close is a missing skill to the skills the person has? One cheap call per target skill, cached in
the store per pair, so every pair is asked only once.

Thirty postings can need 80 and more of these calls, so up to `workers` run at the same time (LLM.call_many): the
first alone, so it writes the cached prefix, then the rest. The answers are read and stored here, in the order of the
targets, so the store holds the same as after one call after the other.
"""

import logging
from collections.abc import Callable

from skillpose.core.llm import LLM, CallResult
from skillpose.core.models import Relation, RelationAnswer
from skillpose.core.prompts import relation_fixed, wrap
from skillpose.core.vocabulary import Vocabulary

log = logging.getLogger(__name__)


class RelationService:
    def __init__(self, llm: LLM, store, snapshot: str, vocab: Vocabulary, workers: int = 1):
        self.llm = llm
        self.store = store
        self.snapshot = snapshot
        self.vocab = vocab
        self.workers = workers

    def _candidate(self, answered_id: str, unknown: list[str]) -> str | None:
        """Map an answered skill to one of the asked candidates; Claude sometimes answers with the name."""
        if answered_id in unknown:
            return answered_id
        resolved = self.vocab.resolve(answered_id)
        return resolved if resolved in unknown else None

    def _relations(self, target: str, unknown: list[str], answer: RelationAnswer) -> dict[str, Relation]:
        """What an answer says about the asked candidates."""
        new: dict[str, Relation] = {}
        unmapped = False
        for r in answer.related:
            candidate = self._candidate(r.skill_id, unknown)
            if candidate is None and self.vocab.resolve(r.skill_id) is None:
                unmapped = True  # a name we cannot place: maybe one of the candidates, so store no none
                log.warning("relation answer %r for %s matches no candidate", r.skill_id, target)
            elif candidate is None:
                log.debug("relation answer %r for %s was not asked; ignored", r.skill_id, target)
            else:
                new[candidate] = r.relation
        if not unmapped:  # only a fully understood answer may mark the rest as unrelated
            new = {c: new.get(c, "none") for c in unknown}
        return new

    def resolve(self, pairs: dict[str, list[str]],
                progress: Callable[[int, int], None] | None = None) -> dict[tuple[str, str], Relation]:
        """Relations for target -> candidates. Pairs a failed call left unknown are missing (count as none).
        progress(done, total) counts the targets that need a call: before the first call and after each one."""
        known = {target: self.store.relations(target) for target in pairs}
        asks = [(target, unknown) for target, candidates in pairs.items()
                if (unknown := [c for c in candidates if c not in known[target]])]
        done = 0

        def take(index: int, result: CallResult[RelationAnswer]) -> None:
            nonlocal done
            target, unknown = asks[index]
            if result.value is not None:
                new = self._relations(target, unknown, result.value)
                self.store.save_relations(target, new)
                known[target] = known[target] | new
            done += 1
            if progress is not None:
                progress(done, len(asks))

        if asks:
            if progress is not None:
                progress(0, len(asks))
            variables = [wrap("target", target) + "\n" + wrap("candidates", ", ".join(unknown))
                         for target, unknown in asks]
            self.llm.call_many("relation", relation_fixed(self.snapshot), variables, RelationAnswer, take,
                               workers=self.workers)
        out: dict[tuple[str, str], Relation] = {}
        for target, candidates in pairs.items():
            out.update({(target, c): known[target][c] for c in candidates if c in known[target]})
        return out
