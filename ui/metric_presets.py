"""指标预设由注册表派生；用户自定义预设随偏好保存。"""
from app_config import cfg, normalize_metrics, update_values
from utils.metrics import REGISTRY, LEAK_CHECK_PRESET, all_metric_types


def metric_presets():
    builtins = {
        '泄漏排查': list(LEAK_CHECK_PRESET),
        'GDI / USER 专项': [key for key in REGISTRY if key in ('num_gdi_objects', 'num_user_objects', 'num_handles')],
        '内存专项': [key for key in all_metric_types() if key.startswith('memory_')],
        '全部指标': list(all_metric_types()),
    }
    builtins.update({f'自定义：{name}': list(values)
                     for name, values in cfg.get(cfg.metric_presets).items()})
    return builtins


def save_metric_preset(name, metrics):
    name = name.strip()[:40]
    if not name:
        raise ValueError('请填写预设名称')
    presets = dict(cfg.get(cfg.metric_presets))
    if name not in presets and len(presets) >= 30:
        raise ValueError('最多保存 30 个自定义预设')
    presets[name] = normalize_metrics(list(metrics))
    update_values([(cfg.metric_presets, presets)])


def default_metrics(fallback):
    return metric_presets().get(cfg.get(cfg.default_metric_preset), list(fallback))
