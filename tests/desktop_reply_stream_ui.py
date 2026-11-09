"""Incremental reply translation with controlled jobs; no SSH or model calls."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    with tempfile.TemporaryDirectory(prefix="bridge-reply-stream-ui-") as directory:
        data = Path(directory)
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["CLAUDE_BRIDGE_DATA"] = str(data)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtWidgets import QApplication
        import app as desktop
        import core

        config = core.defaults()
        config["auto_translate_reply"] = False
        core.atomic_json(data / "config.json", config)
        qt = QApplication.instance() or QApplication([])
        calls, outputs = [], {}
        fail = {"enabled": False}

        class Translator:
            def translate_reply(self, source, code_blocks=None):
                calls.append((source, list(code_blocks or [])))
                if fail["enabled"]:
                    raise core.BridgeError("Synthetic translation failure")
                value = "译文" + str(len(calls))
                outputs[source] = value
                return {"text": value, "model": "synthetic-model"}

        class Pool:
            def __init__(self): self.pending = []
            def start(self, job): self.pending.append(job)
            def one(self):
                self.pending.pop(0).run()
                qt.processEvents()
            def drain(self):
                for _ in range(30):
                    if not self.pending: return
                    self.one()
                raise AssertionError("Unexpected automatic translation loop")

        pool = Pool()
        with patch("app.QThreadPool") as threads, patch.object(desktop.Window, "refresh"), \
             patch.object(desktop.Window, "translator", return_value=Translator()), \
             patch("app.SSH", side_effect=AssertionError("No SSH")):
            threads.globalInstance.return_value.start.side_effect = pool.start
            window = desktop.Window()
            for timer in (window.timer, window.delivery_timer, window.layout_timer, window.draft_timer): timer.stop()
            window.source.setPlainText("保留我的中文草稿")
            window.english.setPlainText("Keep the command draft")
            attachment = data / "keep.txt"
            attachment.write_text("keep", encoding="utf-8")
            window.add_attachments([str(attachment)])
            draft = (window.source.toPlainText(), window.english.toPlainText(), list(window.attachments))
            first = "The first complete paragraph is available now."
            second = "The second complete paragraph arrives while translation is running."
            tail = "An unfinished tail"
            context = "a" * 64
            window.accept_reply({"text": first + "\n\n" + tail, "generating": True, "context": context})
            window.reply_translate.setChecked(True)
            window.auto_refresh.setChecked(False)
            assert len(pool.pending) == 1 and not window.busy
            window.accept_reply({"text": first + "\n\n" + second + "\n\n" + tail,
                                 "generating": True, "context": context})
            assert len(pool.pending) == 1, "Only one translation request may run at once"
            pool.one()
            assert calls[0][0] == first
            assert window.terminal.toPlainText().startswith(outputs[first] + "\n\n")
            assert second in window.terminal.toPlainText() or outputs.get(second, "absent") in window.terminal.toPlainText()
            pool.drain()
            assert calls == [(first, []), (second, [])]
            rendered = window.terminal.toPlainText()
            assert outputs[first] in rendered and outputs[second] in rendered and rendered.endswith(tail)
            for _ in range(4):
                window.accept_reply({"text": first + "\n\n" + second + "\n\n" + tail,
                                     "generating": True, "context": context})
            assert not pool.pending and len(calls) == 2
            window.reply_translate.setChecked(False)
            assert window.terminal.toPlainText() == window.reply_original
            window.reply_translate.setChecked(True)
            window.auto_refresh.setChecked(False)
            assert window.terminal.toPlainText() == rendered and not pool.pending
            window.accept_reply({"text": window.reply_original, "generating": False, "context": context})
            pool.drain()
            assert calls[-1][0] == tail and len(calls) == 3
            completed = window.terminal.toPlainText()
            assert tail not in completed and window.reply_stream.complete
            window.accept_reply({"text": window.reply_original, "generating": False, "context": context})
            assert not pool.pending and window.terminal.toPlainText() == completed

            # In-flight result from the previous native conversation cannot appear.
            old = "This older native conversation must never replace the new one."
            new = "This is the currently selected native conversation."
            window.accept_reply({"text": old, "generating": False, "context": context})
            assert len(pool.pending) == 1
            window.accept_reply({"text": new, "generating": False, "context": "b" * 64})
            window.reply_translate.setChecked(False)
            pool.one()
            assert window.terminal.toPlainText() == new and not pool.pending
            window.reply_translate.setChecked(True)
            window.auto_refresh.setChecked(False)
            pool.drain()
            assert outputs[old] not in window.terminal.toPlainText()
            assert outputs[new] == window.terminal.toPlainText()

            # A setting change rejects an old in-flight translation, even with identical source.
            changed = "This paragraph is translated under the selected model settings."
            window.accept_reply({"text": changed, "generating": False, "context": "b" * 64})
            assert len(pool.pending) == 1
            pid = window.config["provider"]
            original_model = window.config["profiles"][pid]["model"]
            window.config["profiles"][pid]["model"] = original_model + "-synthetic-change"
            window.reply_translate.setChecked(False)
            pool.one()
            old_translation = outputs[changed]
            window.reply_translate.setChecked(True)
            window.auto_refresh.setChecked(False)
            assert old_translation not in window.terminal.toPlainText()
            pool.drain()
            assert window.terminal.toPlainText() == outputs[changed] != old_translation

            # Do not retry a failed chunk automatically when its growing tail changes.
            failing = "A completed sentence which will fail to translate during generation."
            window.accept_reply({"text": failing + "\n\nStill growing", "generating": True,
                                 "context": "c" * 64})
            fail["enabled"] = True
            pool.one()
            failed_count = len(calls)
            for suffix in (" and growing", " and growing further", ". More output arrived."):
                window.accept_reply({"text": failing + "\n\nStill growing" + suffix,
                                     "generating": True, "context": "c" * 64})
            assert not pool.pending and len(calls) == failed_count
            assert "本段翻译失败" in window.reply_translation_status.text()
            # A genuinely replaced latest reply in the same native context is
            # not poisoned by the failure at offset zero of the older reply.
            fail["enabled"] = False
            replaced = "A different latest reply in this same conversation is now ready."
            window.accept_reply({"text": replaced, "generating": False, "context": "c" * 64})
            assert len(pool.pending) == 1
            pool.drain()
            assert window.terminal.toPlainText() == outputs[replaced]
            fail["enabled"] = True
            window.accept_reply({"text": failing + "\n\nStill growing", "generating": True,
                                 "context": "d" * 64})
            pool.one()
            assert "本段翻译失败" in window.reply_translation_status.text()
            fail["enabled"] = False
            window.reply_translate.setChecked(False)
            window.reply_translate.setChecked(True)
            window.auto_refresh.setChecked(False)
            assert len(pool.pending) == 1
            pool.drain()
            assert len(calls) > failed_count
            # A previously cached full snapshot is also an accepted original
            # prefix. Further native growth must only translate its new tail.
            cached_source = "This complete paragraph already has a cached Chinese translation."
            cached_text = "这一段已经有缓存的中文译文。"
            cached_key = desktop.translation_key(cached_source, window.config["profiles"][pid], pid, [])
            window.reply_cache.put(cached_key, cached_source, {"text": cached_text, "model": "cached-model"})
            calls_before_cache = len(calls)
            window.accept_reply({"text": cached_source, "generating": True, "context": "e" * 64})
            assert not pool.pending and window.reply_stream.consumed == len(cached_source)
            assert window.terminal.toPlainText() == cached_text
            appended = "Only this newly appended complete sentence needs translation."
            window.accept_reply({"text": cached_source + "\n\n" + appended,
                                 "generating": True, "context": "e" * 64})
            pool.drain()
            assert calls[calls_before_cache:] == [(appended, [])]
            assert window.terminal.toPlainText() == cached_text + "\n\n" + outputs[appended]
            # Invalid imported settings remain a visible original-text state;
            # they cannot crash read rendering or initiate a request.
            before_invalid = len(calls)
            window.config["profiles"][pid]["reasoning_effort"] = "ultra"
            window.accept_reply({"text": "Invalid configuration still displays this latest reply.",
                                 "generating": True, "context": "d" * 64})
            assert "翻译设置不可用" in window.reply_translation_status.text()
            assert window.terminal.toPlainText() == window.reply_original
            assert not pool.pending and len(calls) == before_invalid
            assert (window.source.toPlainText(), window.english.toPlainText(), window.attachments) == draft
            window.close()
            qt.processEvents()
        print("Reply stream UI: live new chunks, append-safe jobs, toggle/cache, context/settings guards, failure retry and command drafts OK")


if __name__ == "__main__":
    main()
