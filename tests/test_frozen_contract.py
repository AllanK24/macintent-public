"""Regression pins for the current request, model and capture contracts."""

from dataclasses import asdict
import hashlib
import json

from macintent import capture, contract, planner, server, speech


def test_current_contract_pins():
    values = {
        "system_prompt": (contract.SYSTEM_PROMPT, "bf13928dc5960bbca26b8d6c42f61c12336389ceafdbbad3ca9c688287fc75fa"),
        "python_source_rules": (contract.PYTHON_SOURCE_RULES, "e7840a1ccf6ccaa752719a60d0a4c9734605f562b95592857d84197b2fb2e572"),
        "tools": (contract.TOOLS, "cba7547f13824f68eb192aeb87a08a9ebf79eab68a891cc27b0edd7fd247ee7c"),
        "request_settings": (dict(planner.REQUEST_SETTINGS), "7311352ca1b776b7a7f539eeac8dea7003c326f1dbca7431b903e697e454d9eb"),
        "model": (planner.MODEL_ID, "15d47931b70b55af28c0eb5141c48b0a2ac10ec21534e1d48d10fad8a2e5456a"),
        "request_timeout": (planner.REQUEST_TIMEOUT_SECONDS, "26c9a96ce053a14dd88a71a4830c9cbed7e1fed7e3f3f8a0b0b6a58f3f0f02e6"),
        "server_disclosure": (server.DISCLOSURE, "44691aa9b9f4b4923766988c97dae3680b42392717fbba28ea789d1a4ea379fb"),
        "parakeet_spec": (asdict(speech.PARAKEET_SPEC), "0b398b5a0288d420ca387725e83b74eccb5b0777c08619915478975e585f65e6"),
        "parakeet_files": (speech.PARAKEET_FILES, "682b681171be7a145e74fde05cad0a0b5b91c79d02836c6c24ca2a9884be9d0f"),
        "capture_request_hex": (capture._REQUEST.hex(), "776435158b2b0e27c1b72a6cf260802e5a8d085193268c974aa1bc00c5ec37ef"),
    }
    for name, (value, expected) in values.items():
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
        assert hashlib.sha256(encoded).hexdigest() == expected, name
