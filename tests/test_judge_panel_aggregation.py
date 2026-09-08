from grokbuild.artifact_judge import Candidate, JudgeOpinion, aggregate_opinions


def candidates():
    return Candidate("A"), Candidate("B")


def test_base_reads_require_ab_and_ba_position_symmetry():
    a, b = candidates()
    aggregate = aggregate_opinions(
        a,
        b,
        (JudgeOpinion("A", "B", "A"), JudgeOpinion("A", "B", "A")),
    )
    assert aggregate.final == "ABSTAIN"


def test_up_to_three_blind_rereads_resolve_only_a_clear_margin():
    a, b = candidates()
    reads = (
        JudgeOpinion("A", "B", "A", invocation_id="1"),
        JudgeOpinion("B", "A", "B", invocation_id="2"),
        JudgeOpinion("A", "B", "A", invocation_id="3"),
        JudgeOpinion("B", "A", "A", invocation_id="4"),
        JudgeOpinion("A", "B", "B", invocation_id="5"),
        JudgeOpinion("B", "A", "A", invocation_id="ignored"),
    )
    aggregate = aggregate_opinions(a, b, reads, extra_reads_max=3, abstain_margin=0.2)
    assert len(aggregate.additional_reads) == 3
    assert aggregate.final == "ABSTAIN"
    decisive = aggregate_opinions(a, b, reads[:-1][:-1], extra_reads_max=2, abstain_margin=0.2)
    assert decisive.final == "A"
