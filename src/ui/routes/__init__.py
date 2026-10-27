"""
Маршруты окна программы — Flask blueprints по разделам интерфейса.

Раньше все маршруты жили в main_ui.py (2400 строк вместе со сбором);
рефакторинг 16.10.2026 разнёс их по разделам без изменения поведения.
Пути и список компаний — src/core/context.py, сбор — src/core/collect.py.
"""


def register_routes(app) -> None:
    """Подключить все разделы к приложению."""
    # — блоки маршрутов —
    from . import shell
    app.register_blueprint(shell.bp)
    from . import parse
    app.register_blueprint(parse.bp)
    from . import diagnostics
    app.register_blueprint(diagnostics.bp)
    from . import reports
    app.register_blueprint(reports.bp)
    from . import feed
    app.register_blueprint(feed.bp)
    from . import registry
    app.register_blueprint(registry.bp)
    from . import catalog
    app.register_blueprint(catalog.bp)
    from . import settings
    app.register_blueprint(settings.bp)
