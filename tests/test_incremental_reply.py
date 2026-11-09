from reply_translations import IncrementalReply


def accept_next(stream, translated="中文段落"):
    chunk = stream.next_chunk()
    assert chunk is not None
    assert stream.accept(chunk, {"text": translated})
    return chunk


def test_growing_reply_keeps_translated_prefix_and_only_plans_new_text():
    first = "The first useful paragraph is ready.\n\n"
    stream = IncrementalReply().sync(first + "The next paragraph", True, "context/profile")
    chunk = accept_next(stream, "第一段已就绪。")
    assert chunk["source"] == first.strip()
    assert stream.render() == "第一段已就绪。\n\nThe next paragraph"
    assert stream.next_chunk() is None
    stream.sync(first + "The next paragraph has now finished.", True, "context/profile")
    second = accept_next(stream, "下一段也已完成。")
    assert second["source"] == "The next paragraph has now finished."
    assert second["offset"] == len(first)
    assert stream.render() == "第一段已就绪。\n\n下一段也已完成。"
    assert stream.complete
    assert stream.next_chunk() is None


def test_inflight_chunk_can_complete_after_append_but_not_after_rewrite():
    source = "A useful sentence has been completed.\n\n"
    stream = IncrementalReply().sync(source, True, "one")
    pending = stream.next_chunk()
    stream.sync(source + "New text is still arriving", True, "one")
    assert stream.accept(pending, {"text": "已译。"})
    changed = stream.next_chunk()
    assert changed is None
    stream.sync(source.replace("useful", "different") + "New text is still arriving", True, "one")
    assert stream.consumed == 0
    assert not stream.accept(pending, {"text": "旧译文"})


def test_rewrite_retains_only_whole_matching_segments():
    a, b = "The first paragraph is ready.\n\n", "The second paragraph is ready.\n\n"
    stream = IncrementalReply().sync(a, True, "one")
    accept_next(stream, "第一段。")
    stream.sync(a + b, True, "one")
    accept_next(stream, "第二段。")
    stream.sync(a + b + "Unfinished tail", True, "one")
    assert stream.consumed == len(a + b)
    stream.sync(a + b.replace("second", "revised") + "Changed tail", True, "one")
    assert stream.consumed == len(a)
    assert stream.render().startswith("第一段。\n\nThe revised")


def test_context_profile_or_settings_change_rejects_late_chunk():
    source = "A complete and useful English sentence."
    stream = IncrementalReply().sync(source, True, "context/profile/rev1")
    pending = stream.next_chunk()
    stream.sync(source, True, "context/profile/rev2")
    assert not stream.accept(pending, {"text": "过期译文"})
    assert stream.render() == source


def test_final_flush_translates_short_or_unpunctuated_tail():
    stream = IncrementalReply().sync("Almost done", True, "one")
    assert stream.next_chunk() is None
    stream.sync("Almost done", False, "one")
    accept_next(stream, "即将完成")
    assert stream.complete and stream.render() == "即将完成"


def test_open_fence_waits_without_hiding_completed_prose():
    prose = "The explanation has completed first.\n\n"
    open_code = "```python\n" + "print('literal')\n" * 70
    stream = IncrementalReply().sync(prose + open_code, True, "one")
    first = accept_next(stream, "说明已完成。")
    assert first["raw_source"] == prose
    assert stream.next_chunk() is None
    assert stream.render() == "说明已完成。\n\n" + open_code
    stream.sync(prose + open_code + "```\n\n", True, "one")
    chunk = stream.next_chunk()
    assert chunk is not None and chunk["source"] == (open_code + "```").strip()
    assert stream.accept(chunk, {"text": chunk["source"]})
    assert stream.next_chunk() is None


def test_raw_native_code_block_is_atomic_and_forwarded_to_translator():
    prose = "The completed introductory explanation.\n\n"
    code = "print('exact literal')\n" * 65
    source = prose + code + "\nThe final useful paragraph is ready."
    stream = IncrementalReply().sync(source, True, "one", [code])
    first = accept_next(stream)
    assert first["source"] == prose.strip()
    literal = stream.next_chunk()
    assert code.strip() in literal["source"]
    # Native blocks can end in a newline; the trimmed request still must carry
    # the entire code as a protected literal (without outer whitespace).
    assert literal["source"].startswith(code.strip())
    assert literal["code_blocks"] == [code.rstrip()]
    assert stream.accept(literal, {"text": literal["source"]})


def test_inline_code_url_and_quoted_literals_are_not_split():
    code = "`" + "some_literal_" * 70 + "`"
    url = "https://example.test/" + "abc" * 300
    quoted = '"' + "literal words " * 70 + '"'
    for literal in (code, url, quoted):
        stream = IncrementalReply().sync(literal + "\n\nNext useful sentence is available.", True, "one")
        chunk = stream.next_chunk()
        assert literal in chunk["source"]


def test_unfinished_inline_code_waits_for_its_closing_marker():
    prose = "The introductory paragraph is ready.\n\n"
    literal = "``" + "literal words " * 60
    stream = IncrementalReply().sync(prose + literal, True, "one")
    accept_next(stream, "前言。")
    assert stream.next_chunk() is None
    stream.sync(prose + literal + "``\n\n", True, "one")
    assert stream.next_chunk()["source"] == literal + "``"


def test_word_boundary_caps_long_prose_and_never_repeats_consumed_text():
    source = "word " * 500
    stream = IncrementalReply().sync(source, False, "one")
    originals = []
    while chunk := stream.next_chunk():
        assert len(chunk["raw_source"]) <= stream.MAX_CHUNK
        originals.append(chunk["raw_source"])
        assert stream.accept(chunk, {"text": "译文"})
    assert "".join(originals) == source
    assert stream.complete


def test_boundary_whitespace_is_preserved_and_whitespace_only_is_free():
    source = "  A useful complete sentence is ready.\n\n"
    stream = IncrementalReply().sync(source, True, "one")
    accept_next(stream, "  中文。\n")
    assert stream.render() == "  中文。\n\n"
    stream.sync(source + " \n", False, "one")
    assert stream.next_chunk() is None
    assert stream.complete
    assert stream.render() == "  中文。\n\n \n"


def test_request_snapshot_is_detached_and_invalid_results_do_not_advance():
    source = "A useful complete English paragraph."
    stream = IncrementalReply().sync(source, True, "one", ["literal"])
    pending = stream.next_chunk()
    stream.code_blocks.append("later")
    assert "later" not in pending["code_blocks"]
    assert not stream.accept(pending, {"text": ""})
    assert stream.consumed == 0
    assert not stream.accept({**pending, "offset": 0.0}, {"text": "中文"})


def test_whole_cache_seeds_long_prefix_and_append_only_plans_new_tail():
    source = "  " + "Original words " * 120 + "\n\n"
    stream = IncrementalReply().sync(source, True, "context/profile")
    cached = {"text": "  已缓存的完整译文。 \n\n"}
    assert stream.seed_cached(cached)
    assert stream.complete and stream.consumed == len(source)
    assert stream.render() == cached["text"]
    assert stream.scope == "context/profile" and stream.source == source
    stream.sync(source + "The next complete sentence arrives.", True, "context/profile")
    chunk = stream.next_chunk()
    assert chunk["offset"] == len(source)
    assert chunk["source"] == "The next complete sentence arrives."
    assert stream.render().startswith(cached["text"])


def test_whole_cache_rejects_invalid_or_existing_partial_state():
    stream = IncrementalReply().sync("First useful complete sentence.\n\nTail", True, "one")
    for result in (None, {}, {"text": None}, {"text": " \n"}):
        assert not stream.seed_cached(result)
    accept_next(stream, "第一句。")
    before = (stream.consumed, stream.render(), stream.source, stream.scope)
    assert not stream.seed_cached({"text": "不覆盖部分译文"})
    assert before == (stream.consumed, stream.render(), stream.source, stream.scope)
    assert not IncrementalReply().sync(" \n", False, "one").seed_cached({"text": "中文"})


def test_cached_prefix_rewrite_and_scope_guards_match_normal_chunks():
    source = "A useful complete original reply."
    stream = IncrementalReply().sync(source, False, "one")
    assert stream.seed_cached({"text": "原回复。"})
    assert not stream.seed_cached({"text": "重复"})
    stream.sync(source.replace("original", "edited"), False, "one")
    assert stream.consumed == 0
    assert stream.render() == source.replace("original", "edited")
    assert stream.seed_cached({"text": "已改回复。"})
    stream.sync(stream.source, False, "two")
    assert stream.consumed == 0


def test_current_turn_progress_rows_drain_in_order_when_later_rows_arrive_inflight():
    # Native capture joins assistant progress rows from the same current user
    # turn. New rows append to this source even when tool rows sit between them.
    # Each row fits a chunk, but two rows together exceed the normal chunk cap.
    rows = [f"Progress {number}: " + (f"detail{number} " * 48) + "This update is complete."
            for number in range(1, 5)]
    stream = IncrementalReply().sync(rows[0], True, "same-task/profile")
    sources = []
    first = stream.next_chunk()
    assert first["source"] == rows[0]
    sources.append(first["raw_source"])
    assert stream.accept(first, {"text": "进度1"})

    stream.sync("\n\n".join(rows[:2]), True, "same-task/profile")
    second = stream.next_chunk()
    assert second["source"] == rows[1]
    # The second model call is still running when progress rows 3 and 4 arrive.
    stream.sync("\n\n".join(rows), True, "same-task/profile")
    sources.append(second["raw_source"])
    assert stream.accept(second, {"text": "进度2"})
    for number in (3, 4):
        chunk = stream.next_chunk()
        assert chunk["source"] == rows[number - 1]
        assert chunk["offset"] == sum(map(len, sources))
        sources.append(chunk["raw_source"])
        assert stream.accept(chunk, {"text": f"进度{number}"})
    assert "".join(sources) == "\n\n".join(rows)
    assert stream.render() == "\n\n".join(f"进度{number}" for number in range(1, 5))
    assert stream.complete and stream.next_chunk() is None


def test_current_turn_short_completed_rows_can_batch_without_skipping_middle():
    rows = [f"Progress {number}: the completed update is available." for number in range(1, 5)]
    source = "\n\n".join(rows[:3])
    stream = IncrementalReply().sync(source, True, "same-task/profile")
    batch = stream.next_chunk()
    assert batch["offset"] == 0
    assert batch["source"] == source
    assert stream.accept(batch, {"text": "进度1\n\n进度2\n\n进度3"})
    stream.sync("\n\n".join(rows), True, "same-task/profile")
    last = stream.next_chunk()
    assert last["offset"] == len(source)
    assert last["source"] == rows[3]
    assert stream.accept(last, {"text": "进度4"})
    assert stream.render() == "进度1\n\n进度2\n\n进度3\n\n进度4"


def test_current_turn_new_rows_do_not_advance_past_unaccepted_earlier_content():
    first = "The earlier progress update is complete."
    later = "A newer progress update has now appeared."
    stream = IncrementalReply().sync(first, True, "same-task/profile")
    pending = stream.next_chunk()
    stream.sync(first + "\n\n" + later, True, "same-task/profile")
    retry_or_batch = stream.next_chunk()
    assert stream.consumed == 0
    assert retry_or_batch["offset"] == pending["offset"] == 0
    assert retry_or_batch["source"].startswith(first)
    assert stream.render() == first + "\n\n" + later


def test_current_turn_row_boundary_flushes_earlier_prose_before_newer_rows():
    first = "Earlier progress without a final punctuation mark"
    stream = IncrementalReply().sync(first, True, "same-task/profile")
    assert stream.next_chunk() is None
    later = "A later complete assistant progress update."
    stream.sync(first + "\n\n" + later, True, "same-task/profile")
    chunk = stream.next_chunk()
    # Batching is allowed, but a later complete sentence cannot jump over the
    # preceding row simply because that row lacked sentence punctuation.
    assert chunk["offset"] == 0
    assert chunk["source"].startswith(first)
    assert first in chunk["source"] and later in chunk["source"]


def test_current_turn_old_row_rewrite_rejects_newer_row_pending_result():
    first = "The first assistant progress update is complete."
    second = "The second assistant progress update is complete."
    stream = IncrementalReply().sync(first, True, "same-task/profile")
    accept_next(stream, "第一条进度。")
    stream.sync(first + "\n\n" + second, True, "same-task/profile")
    pending_second = stream.next_chunk()
    revised = first.replace("first", "revised first")
    stream.sync(revised + "\n\n" + second, True, "same-task/profile")
    assert stream.consumed == 0
    assert not stream.accept(pending_second, {"text": "不应跳过改写后的第一条"})
    assert stream.next_chunk()["source"].startswith(revised)
