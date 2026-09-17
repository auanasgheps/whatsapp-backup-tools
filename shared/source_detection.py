import os


WA_DATABASES_DIR_NAME = "Whatsapp Databases"


def detect_source(output_root: str):
    """
    Detect source type and DB path from output_root.
    Returns (source_type, db_path) where source_type is 'android', 'ios', or None.
    """
    wa_db_dir = os.path.join(output_root, WA_DATABASES_DIR_NAME)
    for base_dir in (wa_db_dir, output_root):
        msgstore = os.path.join(base_dir, "msgstore.db")
        chat_storage = os.path.join(base_dir, "ChatStorage.sqlite")
        if os.path.exists(msgstore):
            return ("android", msgstore)
        if os.path.exists(chat_storage):
            return ("ios", chat_storage)
    return (None, None)

