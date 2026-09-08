# Fixture quality records

Public task fixtures contain only the task contract and references to controller-owned grading material. The controller stores the per-task quality record with the fixture review evidence; it is not copied into the public fixture or tested worktree.

Before publication, two reviewers independently check each fixture for:

1. **Depersonalization:** no people, organizations, repository names, identifying project facts, or proprietary facts.
2. **Rubric clarity:** the prompt and acceptance criteria distinguish complete, partial, and incorrect outcomes.
3. **Verifier validity:** the controller-owned hidden verifier discriminates a correct solution from a plausible incorrect one and has a reviewed expected outcome.

The controller records reviewer identities, fixture and rubric versions, decisions, evidence references, and freeze time in its private quality record. This README documents the process only; it is not the quality record.

Hidden holdout cases and verifier source remain in immutable controller-owned storage outside the repository. Actor capability views are disjoint: proposers cannot apply, appliers cannot read holdout data, judges cannot mutate or run evidence collection, and only the read-only grader receives the external holdout mount with network disabled. Boundary verification fails closed.
