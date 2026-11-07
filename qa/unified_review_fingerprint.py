"""Отпечаток содержимого администраторского обзора, не подпись и не аутентификация."""
import hashlib
import json


def review_fingerprint(document_id, actor_id, company_id, doc_values, origins, item_hashes, pool_state):
    """Связать решение с просмотренными снимками и контекстом доступа."""
    data = [document_id, actor_id, company_id, list(doc_values),
            sorted([list(row) for row in origins]), sorted([list(row) for row in item_hashes]),
            list(pool_state)]
    value = json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(value.encode('utf-8')).hexdigest()
