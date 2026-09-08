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


def test_all_admitted_reads_are_validated_before_statistics():
    a, b = candidates()
    malformed_sets = (
        (
            JudgeOpinion("A", "B", "A", invocation_id="1"),
            JudgeOpinion("B", "A", "not-a-candidate", invocation_id="2"),
        ),
        (
            JudgeOpinion("A", "B", "A", invocation_id="1"),
            JudgeOpinion("B", "A", "A", invocation_id="2"),
            JudgeOpinion("A", "C", "A", invocation_id="3"),
        ),
        (
            JudgeOpinion("A", "B", "A", invocation_id="same"),
            JudgeOpinion("B", "A", "A", invocation_id="2"),
            JudgeOpinion("A", "B", "A", invocation_id="same"),
        ),
        (
            JudgeOpinion("A", "B", "A", confidence=float("nan"), invocation_id="1"),
            JudgeOpinion("B", "A", "A", invocation_id="2"),
        ),
    )
    for reads in malformed_sets:
        aggregate = aggregate_opinions(a, b, reads)
        assert aggregate.final == "ABSTAIN"
        assert aggregate.margin == 0.0
        assert aggregate.repeatability == 0.0
        assert aggregate.position_bias is None


def test_out_of_range_confidence_invalidates_the_comparison():
    a, b = candidates()
    for confidence in (-10.0, 50.0):
        aggregate = aggregate_opinions(
            a,
            b,
            (
                JudgeOpinion("A", "B", "A", confidence=confidence, invocation_id="1"),
                JudgeOpinion("B", "A", "A", confidence=0.8, invocation_id="2"),
            ),
        )
        assert aggregate.final == "ABSTAIN"
        assert aggregate.margin == aggregate.repeatability == 0.0


def test_all_abstentions_have_zero_statistics_without_division_by_zero():
    a, b = candidates()
    aggregate = aggregate_opinions(
        a,
        b,
        (
            JudgeOpinion("A", "B", "abstain", invocation_id="1"),
            JudgeOpinion("B", "A", "abstain", invocation_id="2"),
        ),
    )
    assert aggregate.final == "ABSTAIN"
    assert aggregate.margin == aggregate.repeatability == 0.0
    assert aggregate.position_bias is None


def test_position_bias_compares_candidate_rates_conditioned_on_order():
    a, b = candidates()
    unbiased = aggregate_opinions(
        a,
        b,
        (
            JudgeOpinion("A", "B", "A", invocation_id="1"),
            JudgeOpinion("B", "A", "A", invocation_id="2"),
        ),
    )
    biased = aggregate_opinions(
        a,
        b,
        (
            JudgeOpinion("A", "B", "A", invocation_id="1"),
            JudgeOpinion("B", "A", "B", invocation_id="2"),
        ),
    )
    assert unbiased.position_bias == 0.0
    assert biased.position_bias == 1.0
