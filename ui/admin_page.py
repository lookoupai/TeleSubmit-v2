"""Shared server-rendered layout for the management pages."""
import html
from pathlib import Path

_assets = Path(__file__).with_name("admin")
_styles = (_assets / "admin.css").read_text(encoding="utf-8")
_script = (_assets / "admin.js").read_text(encoding="utf-8")

NAVIGATION = (
    ("工作台", (("", "概览"), ("telegram-ui", "Telegram 功能"))),
    ("投稿与审核", (("submit", "投稿设置"), ("whitelist", "投稿白名单"), ("ai", "AI 审核"))),
    ("频道运营", (("schedule", "定时发布"), ("fallback", "兜底定时"))),
    ("广告管理", (("slots", "广告位"), ("ads", "广告参数"))),
)


def render_admin_page(*, title, body, brand, base, server_time):
    base = base.rstrip("/")
    title_text = html.escape(title)
    brand_text = html.escape(brand)
    brand_copy = (
        f'<span class="brand-copy"><b>{html.escape(brand.removesuffix("管理后台").strip())}</b><small>管理后台</small></span>'
        if brand.endswith("管理后台") and brand != "管理后台"
        else f'<span class="brand-copy"><b>{brand_text}</b></span>'
    )
    base_url = html.escape(base, quote=True)
    login = title in {"登录", "登录失败"}
    navigation = ""
    for group, links in NAVIGATION:
        items = "".join(
            f'<a href="{base_url}{"/" + path if path else ""}" data-nav>'
            f'{html.escape(label)}</a>'
            for path, label in links
        )
        navigation += f'<div class="nav-group"><p>{group}</p>{items}</div>'
    chrome = "" if login else f"""
    <aside id="sidebar" class="sidebar" aria-label="后台导航">
      <a class="brand" href="{base_url}">
        <span class="brand-mark" aria-hidden="true">T</span>
        {brand_copy}
      </a>
      <nav>{navigation}</nav>
      <div class="sidebar-footer">
        <label for="theme">外观</label>
        <select id="theme" aria-label="外观">
          <option value="system">跟随系统</option>
          <option value="light">浅色</option>
          <option value="dark">深色</option>
        </select>
        <a class="logout" href="{base_url}/logout">退出登录 <span aria-hidden="true">↗</span></a>
      </div>
    </aside>
    <button class="menu-shade" type="button" aria-label="关闭导航" hidden></button>
    <header class="topbar">
      <button class="menu-toggle" type="button" aria-controls="sidebar" aria-expanded="false">菜单</button>
      <div class="breadcrumb"><span>管理后台</span><span aria-hidden="true">/</span><strong>{title_text}</strong></div>
      <span class="server-time">服务器时间 <time>{html.escape(server_time)}</time></span>
    </header>"""
    login_brand = f'<div class="login-brand"><span class="brand-mark" aria-hidden="true">T</span>{brand_text}</div>' if login else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>{title_text} · {brand_text}</title>
  <script>document.documentElement.classList.add('js');</script>
  <style>{_styles}</style>
</head>
<body class="{"login-page" if login else "admin-page"}">
  <a class="skip-link" href="#main">跳到页面内容</a>
  {chrome}
  <main id="main" class="page-content" tabindex="-1">
    {login_brand}{body}
  </main>
  <script>{_script}</script>
</body>
</html>"""
