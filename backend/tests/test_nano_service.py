import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from fastapi import HTTPException


def service_module(tmp_path):
    path = Path(__file__).resolve().parents[2] / "chatterbox-service" / "app.py"
    spec = importlib.util.spec_from_file_location("nano_service_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.VOICE_ROOT = tmp_path
    return module


def test_conditioning_cached_and_isolated(tmp_path):
    module = service_module(tmp_path)
    for user in ("alice", "bob"):
        (tmp_path / user).mkdir()
        (tmp_path / user / "voice.wav").write_bytes(b"mock reference")
    model = Mock()
    model.sr = 24000
    model.generate.return_value.squeeze.return_value.detach.return_value.cpu.return_value.numpy.return_value = np.zeros(24000)
    model.prepare_conditionals.side_effect = lambda path: setattr(model, "conds", path)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(model=model)))
    module.synthesize(module.SpeechRequest(text="hello", voice="alice/voice.wav"), request)
    module.synthesize(module.SpeechRequest(text="hello", voice="bob/voice.wav"), request)
    module.synthesize(module.SpeechRequest(text="hello", voice="alice/voice.wav"), request)
    assert model.prepare_conditionals.call_count == 2
    assert model.conds == str(tmp_path / "alice" / "voice.wav")


def test_service_rejects_loading_and_path_escape(tmp_path):
    module = service_module(tmp_path)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(model=None)))
    with pytest.raises(HTTPException) as failure:
        module.synthesize(module.SpeechRequest(text="hello", voice="u/voice.wav"), request)
    assert failure.value.status_code == 503
    with pytest.raises(HTTPException) as failure:
        module.reference_path("u/../../secret.wav")
    assert failure.value.status_code == 400
