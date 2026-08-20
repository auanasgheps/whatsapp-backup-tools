import os


def detect_source(output_root: str):
    """
    Detect source type and DB path from output_root.
    Returns (source_type, db_path) where source_type is 'android', 'ios', or None.
    """
    msgstore = os.path.join(output_root, "msgstore.db")
    chat_storage = os.path.join(output_root, "ChatStorage.sqlite")
    if os.path.exists(msgstore):
        return ("android", msgstore)
    if os.path.exists(chat_storage):
        return ("ios", chat_storage)
    return (None, None)
