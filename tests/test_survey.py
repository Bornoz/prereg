from prereg import survey


def test_the_shape_of_a_message_survives_only_its_template():
    a = "[TOPLOC Trace #6151] Validated layer weights DA for Qwen2.5 (integrity: 99.4%)"
    b = "[TOPLOC Trace #6231] Validated layer weights DA for Qwen2.5 (integrity: 99.4%)"
    assert survey.shape(a) == survey.shape(b)


def test_emoji_decoration_does_not_make_a_line_look_new():
    # The clone family varies a leading emoji over a fixed sentence pool.
    assert survey.shape("a day on venus is longer than its year") == survey.shape(
        "\U0001f331 a day on venus is longer than its year"
    )


def test_addresses_dids_and_urls_all_collapse():
    assert survey.shape("sent to 0xdeadbeefcafe1234") == survey.shape(
        "sent to 0xfeedface99887766"
    )
    assert survey.shape("see https://a.example/x") == survey.shape("see https://b.test/y")


def test_genuinely_different_sentences_keep_different_shapes():
    assert survey.shape("the queue is clearing fast today") != survey.shape(
        "anyone running anything heavy through it"
    )


def room(name, sampled, writers, shapes, top=1):
    return survey.RoomSurvey(room=name, topic=None, sampled=sampled, writers=writers,
                             shapes=shapes, top_shape="x", top_shape_count=top)


def test_many_keys_one_script_is_named_as_such():
    # The failure mode that defeats nick_diversity: a fresh key per message.
    verdict = room("swarm", 200, writers=199, shapes=4).verdict
    assert verdict == "many keys, one script"


def test_a_single_bot_in_a_loop_is_named_separately():
    assert room("loop", 200, writers=2, shapes=6).verdict == "one bot in a loop"


def test_a_real_conversation_reads_as_varied():
    assert room("lobby", 200, writers=180, shapes=190).verdict == "varied"


def test_a_tiny_sample_is_not_judged():
    assert room("new", 4, writers=1, shapes=1).verdict == "too small to judge"


def test_a_clone_family_needs_a_shared_template_and_matching_sizes():
    listed = [
        {"room": f"node{i}", "topic": f"node{i} — node", "bytes": 8_000_000 + i * 1_000}
        for i in range(5)
    ]
    families = survey.clone_families(listed)
    assert len(families) == 1
    assert len(families[0][1]) == 5


def test_rooms_of_wildly_different_sizes_are_not_a_family():
    listed = [
        {"room": "a", "topic": "a — node", "bytes": 1_000},
        {"room": "b", "topic": "b — node", "bytes": 500_000},
        {"room": "c", "topic": "c — node", "bytes": 9_000_000},
    ]
    assert survey.clone_families(listed) == []


def test_two_rooms_are_not_enough_to_call_a_family():
    listed = [{"room": f"n{i}", "topic": f"n{i} — node", "bytes": 8_000_000} for i in range(2)]
    assert survey.clone_families(listed) == []


def test_rooms_without_a_topic_are_ignored():
    assert survey.clone_families([{"room": "x", "topic": None, "bytes": 10}]) == []
