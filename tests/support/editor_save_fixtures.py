from uuid import uuid4

from migration_harness.runs.editor_queue import editor_save_intent, record_queued_editor_save


def pending_editor_edit(queue, target, edit):
    intent = editor_save_intent(target, edit["base"], edit["content"], None, False)
    record_queued_editor_save(queue, edit, uuid4().hex, intent)
    queue["edits"][target] = edit
