"""Waiting questions stay visible and copy never translates or sends a task."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    with tempfile.TemporaryDirectory(prefix="bridge-question-copy-ui-") as directory:
        data = Path(directory)
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["CLAUDE_BRIDGE_DATA"] = str(data)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QPalette
        import app as desktop
        import core

        config = core.defaults()
        config["auto_translate_reply"] = False
        core.atomic_json(data / "config.json", config)
        qt = QApplication.instance() or QApplication([])
        qt.setStyle("Fusion")
        qt.setStyleSheet(desktop.STYLE)
        calls = []

        class Translator:
            def translate_reply(self, source, code_blocks=None):
                calls.append(source)
                return {"text": "已翻译的问题和全部选项。", "model": "synthetic"}

        class Pool:
            def __init__(self): self.pending = []
            def start(self, job): self.pending.append(job)
            def drain(self):
                for _ in range(30):
                    if not self.pending: return
                    self.pending.pop(0).run()
                    qt.processEvents()
                raise AssertionError("Unchanged questions must not cause a translation loop")

        pool = Pool()
        with patch("app.QThreadPool") as threads, patch.object(desktop.Window, "refresh"), \
             patch.object(desktop.Window, "translator", return_value=Translator()), \
             patch("app.SSH", side_effect=AssertionError("Question display/copy must never click or send")):
            threads.globalInstance.return_value.start.side_effect = pool.start
            window = desktop.Window()
            for timer in (window.timer, window.delivery_timer, window.layout_timer, window.draft_timer): timer.stop()
            window.source.setPlainText("保留中文草稿")
            draft = "First line.\nSecond edited line.\n"
            window.english.setPlainText(draft)
            cursor = window.english.textCursor()
            cursor.setPosition(1)
            cursor.setPosition(5, cursor.MoveMode.KeepAnchor)
            window.english.setTextCursor(cursor)
            before = (window.source.toPlainText(), window.english.toPlainText())
            qt.clipboard().setText("previous clipboard")
            window.copy_english_button.click()
            assert qt.clipboard().text() == draft, "Copy the complete edited draft, not its selected fragment"
            assert "已复制" in window.statusBar().currentMessage()
            assert (window.source.toPlainText(), window.english.toPlainText()) == before
            window.english.clear()
            window.copy_english_button.click()
            assert qt.clipboard().text() == draft, "Empty drafts do not erase the clipboard"
            window.english.setPlainText(draft)
            context = "a" * 64
            text = "Choose a voice.\n\n1. Man, 45-55\nWarm storyteller.\n\n2. Woman, 40-55\nWarm female voice."
            result = {"context": context, "mode": "Code", "text": text, "generating": True,
                      "awaiting_input": True, "question": {"prompt": "Choose a voice.", "page": 1, "total_pages": 3,
                      "options": [{"label": "Man, 45-55", "description": "Warm storyteller.", "selected": True},
                                  {"label": "Woman, 40-55", "description": "Warm female voice.", "selected": False}]}}
            window.accept_reply(result)
            assert not window.reply_generating and window.reply_awaiting_input
            assert window.terminal.toPlainText() == text
            assert "请到 Ubuntu 查看并操作" in window.reply_translation_status.text()
            assert window.reply_translation_status.property("attention") is True
            assert window.reply_translation_status.palette().color(QPalette.ColorRole.WindowText).name() == "#956600"
            window.reply_translate.setChecked(True)
            window.auto_refresh.setChecked(False)
            pool.drain()
            assert window.terminal.toPlainText() == "已翻译的问题和全部选项。"
            assert "请到 Ubuntu 查看并操作" in window.reply_translation_status.text()
            paid = len(calls)
            for _ in range(3): window.accept_reply(result)
            pool.drain()
            assert len(calls) == paid
            window.reply_translate.setChecked(False)
            window.accept_reply({"context": context, "text": "", "awaiting_input": True})
            assert window.reply_awaiting_input and "Ubuntu" in window.terminal.placeholderText()
            assert window.reply_translation_status.property("attention") is True
            assert not pool.pending
            window.accept_reply({"context": "b" * 64, "text": "A new task reply."})
            assert not window.reply_awaiting_input and not window.reply_question
            assert "等待你选择" not in window.reply_translation_status.text()
            assert window.reply_translation_status.property("attention") is False
            assert window.reply_translation_status.palette().color(QPalette.ColorRole.WindowText).name() == "#60758c"
            window.reply_read_failed("Synthetic unavailable input card")
            assert "请到 Ubuntu" in window.reply_translation_status.text()
            assert window.reply_translation_status.property("attention") is True
            assert (window.source.toPlainText(), window.english.toPlainText()) == before
            window.close()
            qt.processEvents()
        print("Question/copy UI: full-draft clipboard, no empty overwrite, persistent yellow waiting fallback, translation/cache and context reset OK")


if __name__ == "__main__":
    main()
