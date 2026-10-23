from ttmon.ai.base import Classifier
from ttmon.ai.gemini import GeminiClassifier
from ttmon.logging import register_secret
from ttmon.settings import AppConfig, Settings, project_path

CATEGORIES_PLACEHOLDER = "{categories}"


class AIConfigError(RuntimeError):
    pass


def load_prompt(cfg: AppConfig) -> str:
    text = project_path(cfg.ai.prompt_file).read_text(encoding="utf-8")
    if CATEGORIES_PLACEHOLDER not in text:
        raise AIConfigError(f"в {cfg.ai.prompt_file} нет {CATEGORIES_PLACEHOLDER}")
    return text.replace(CATEGORIES_PLACEHOLDER, "\n".join(f"- {c}" for c in cfg.categories))


def build_classifier(cfg: AppConfig, settings: Settings) -> Classifier:
    match cfg.ai.provider:
        case "gemini":
            api_keys = settings.gemini_api_keys()
            if not api_keys:
                raise AIConfigError("нет GEMINI_API_KEY в .env")
            for key in api_keys:
                register_secret(key)
            return GeminiClassifier(
                api_keys=api_keys,
                model=settings.gemini_model,
                cfg=cfg.ai,
                categories=cfg.categories,
                system_prompt=load_prompt(cfg),
                voice_prompt=project_path(cfg.ai.voice_prompt_file).read_text(encoding="utf-8"),
            )
        case "claude":
            raise AIConfigError("провайдер claude ещё не реализован")
