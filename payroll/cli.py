import click

from . import db, services


def register(app):
    @app.cli.command("seed-demo")
    def seed_demo():
        """引き継ぎ資料のメンバーと計算例（天野さん 8月61件 / 9月20日・50件）を投入する。"""
        conn = db.connect(app.config["DATABASE"])
        if services.list_members(conn):
            click.echo("既にメンバーが登録されているため何もしません")
            return
        for i, name in enumerate(["堀田", "矢木", "天野", "島", "平川"]):
            services.save_member(conn, None, name, "contractor", False,
                                 35000 if name == "堀田" else None,
                                 "35,000円は立替・経費として管理（目安35,000円以内）" if name == "堀田" else "",
                                 True, i + 1)
        amano = conn.execute("SELECT id FROM members WHERE name='天野'").fetchone()["id"]
        services.create_month(conn, "2026-08")
        services.save_record(conn, "2026-08", amano, None, 61, None)
        conn.commit()
        services.create_month(conn, "2026-09")
        services.save_record(conn, "2026-09", amano, 20, 50, None)
        conn.commit()
        services.add_item(conn, "2026-09", amano, "transport", "2026-09", 45000, "ワースリビング分調整後")
        services.add_item(conn, "2026-09", amano, "advance", "2026-09", 10000, "")
        services.add_item(conn, "2026-09", amano, "gasoline", "2026-09", 15000, "ガソリン")
        click.echo("デモデータを投入しました（2026年8月・9月）")
        conn.close()
