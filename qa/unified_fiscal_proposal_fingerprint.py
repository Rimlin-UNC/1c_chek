"""Отпечаток состояния для предложений по фискальным расхождениям.

Это не подпись и не доказательство проверки ФНС.
"""
import hashlib
import json


def fiscal_proposal_fingerprint(document_id, actor_id, company_id, document, source_values,
                                origin_hashes, item_hashes, pool_state):
    data = [document_id, actor_id, company_id, list(document),
            sorted([list(row) for row in source_values]),
            sorted([list(row) for row in origin_hashes]), sorted([list(row) for row in item_hashes]),
            list(pool_state)]
    raw = json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()
