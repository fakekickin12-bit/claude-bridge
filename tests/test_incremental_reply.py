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
