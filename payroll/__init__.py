import os
from datetime import date

from flask import Flask

from . import calc, db, services


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("PAYROLL_SECRET_KEY", "local-dev-only"),
        DATABASE=os.environ.get("PAYROLL_DB", os.path.join(app.instance_path, "payroll.sqlite3")),
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(os.path.dirname(app.config["DATABASE"]) or ".", exist_ok=True)

    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        db.init_db(conn)
        conn.close()

    app.teardown_appcontext(db.close_db)

    @app.template_filter("yen")
    def yen(v):
        return "" if v is None else f"{v:,}"

    @app.context_processor
    def globals_():
        return {
            "CATEGORIES": calc.CATEGORIES,
            "ITEM_TYPES": calc.ITEM_TYPES,
            "month_label": services.month_label,
            "short_month_label": services.short_month_label,
            "today_month": date.today().strftime("%Y-%m"),
        }

    from . import views
    app.register_blueprint(views.bp)

    from . import cli
    cli.register(app)
    return app
