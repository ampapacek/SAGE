MODEL_PRICES_PER_1K = {
    "gpt-5": (0.00125, 0.01),
    "gpt-5-mini": (0.00025, 0.002),
    "gpt-4o-mini": (0.00015, 0.0006),
    "o4-mini": (0.0011, 0.0044),
    "gpt-5-nano": (0.00005, 0.0004),
}


def normalize_model_name(model):
    if not model:
        return ""
    model = model.lower()
    for key in MODEL_PRICES_PER_1K:
        if model == key or model.startswith(f"{key}-"):
            return key
    return model


def get_model_rates(model, default_input_rate, default_output_rate):
    key = normalize_model_name(model)
    if key in MODEL_PRICES_PER_1K:
        return MODEL_PRICES_PER_1K[key]
    return default_input_rate, default_output_rate
