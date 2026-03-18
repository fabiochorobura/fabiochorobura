#!/usr/bin/env python3
"""
update_readme.py — fabiochorobura
==================================
Atualiza automaticamente o README.md do perfil GitHub com:

  1. Dados do LinkedIn: Sobre, Formação Acadêmica, Cursos & Certificados
     - Tenta scraping público do LinkedIn (best-effort)
     - Usa linkedin_data.json como fallback / dados manuais
  2. Commits em repositórios PRÓPRIOS e de TERCEIROS (via GitHub Search Commits API)
  3. Linguagens mais usadas em TODOS os repos com commits (via GitHub Languages API)
  4. Total de commits indexados pelo GitHub Search API

Uso local:
    python update_readme.py

Variáveis de ambiente:
    GITHUB_TOKEN  — Personal Access Token (opcional; aumenta rate limit)

Dependências:
    pip install requests beautifulsoup4
"""

import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ── Configuração ─────────────────────────────────────────────────────────────
GITHUB_USERNAME = "fabiochorobura"
LINKEDIN_URL    = "https://www.linkedin.com/in/fabiochorobura/"
GITHUB_TOKEN    = os.getenv("GITHUB_TOKEN", "")

_DIR          = os.path.dirname(os.path.abspath(__file__))
LINKEDIN_FILE = os.path.join(_DIR, "linkedin_data.json")
README_FILE   = os.path.join(_DIR, "README.md")


# ════════════════════════════ LinkedIn ═══════════════════════════════════════

def _safe_get(url: str, **kwargs):
    """HTTP GET com timeout e captura de exceções."""
    try:
        return requests.get(url, timeout=20, **kwargs)
    except requests.RequestException as exc:
        print(f"  ⚠  Erro de rede: {exc}")
        return None


def _parse_linkedin_html(html: str) -> dict:
    """
    Tenta extrair dados do HTML público do LinkedIn.
    Estratégias (em ordem):
      1. JSON-LD schema.org/Person embutido na página
      2. Tags <meta> name="description" (fallback)
    """
    soup = BeautifulSoup(html, "html.parser")
    data: dict = {}

    # ── JSON-LD ───────────────────────────────────────────────────────────
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            obj = json.loads(tag.string or "")
            if not isinstance(obj, dict):
                continue
            if obj.get("@type") != "Person":
                continue

            data["sobre"] = obj.get("description", "")
            data["nome"]  = obj.get("name", "")

            edu_raw = obj.get("alumniOf", [])
            if isinstance(edu_raw, dict):
                edu_raw = [edu_raw]
            data["formacoes"] = [
                {
                    "instituicao": e.get("name", ""),
                    "curso": e.get("department", e.get("description", "")),
                    "periodo": (
                        f"{e.get('startDate', '')}–{e.get('endDate', '')}"
                        if e.get("startDate") else ""
                    ),
                }
                for e in edu_raw
                if isinstance(e, dict)
            ]
            break
        except (json.JSONDecodeError, AttributeError):
            pass

    # ── meta description (fallback para "sobre") ─────────────────────────
    if not data.get("sobre"):
        meta = soup.find("meta", {"name": "description"})
        if meta and meta.get("content"):
            data["sobre"] = meta["content"]

    return data


def fetch_linkedin() -> dict:
    """
    Tenta obter dados do perfil LinkedIn.
    Retorna dict com os campos extraídos (pode ser vazio se LinkedIn bloquear).
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cache-Control": "no-cache",
    }
    print(f"  Tentando scraping do LinkedIn: {LINKEDIN_URL}")
    resp = _safe_get(LINKEDIN_URL, headers=headers, allow_redirects=True)

    if resp is None or resp.status_code != 200:
        code = getattr(resp, "status_code", "—")
        print(f"  ⚠  LinkedIn retornou status {code} — usando linkedin_data.json")
        return {}

    parsed = _parse_linkedin_html(resp.text)
    if not parsed.get("sobre") and not parsed.get("formacoes"):
        print("  ⚠  HTML do LinkedIn não contém dados úteis — usando linkedin_data.json")
        return {}

    print("  ✓  Dados extraídos do LinkedIn com sucesso.")
    return parsed


def load_linkedin_data() -> dict:
    """
    Carrega dados do LinkedIn com merge entre scraping e arquivo local.
    O arquivo linkedin_data.json tem prioridade (permite ajustes manuais).
    """
    local: dict = {}
    if os.path.exists(LINKEDIN_FILE):
        with open(LINKEDIN_FILE, encoding="utf-8") as fh:
            local = json.load(fh)
        # Remove chave de comentário se existir
        local.pop("_comentario", None)

    scraped = fetch_linkedin()

    # Campos locais sobrescrevem os do scraping quando preenchidos
    merged: dict = {}
    for key in set(scraped) | set(local):
        local_val  = local.get(key)
        scraped_val = scraped.get(key)
        # Prefere local se preenchido, senão usa o scraped
        if local_val and local_val != [] and local_val != {}:
            merged[key] = local_val
        elif scraped_val:
            merged[key] = scraped_val
        else:
            merged[key] = local_val or scraped_val

    return merged


# ════════════════════════════ GitHub ═════════════════════════════════════════

def _gh_headers() -> dict:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def search_own_commits() -> dict:
    """
    Busca commits do usuário em repos PRÓPRIOS via Search Commits API.
    Retorna {repo_full_name: commit_count} — cobre histórico completo (repos públicos).
    """
    h = _gh_headers()
    h["Accept"] = "application/vnd.github.cloak-preview+json"

    repo_commits: dict = {}
    own_prefix = f"{GITHUB_USERNAME.lower()}/"
    for page in range(1, 11):  # máx 10 páginas × 100 = 1000 commits
        url = (
            f"https://api.github.com/search/commits"
            f"?q=author:{GITHUB_USERNAME}+user:{GITHUB_USERNAME}"
            f"&sort=author-date&order=desc"
            f"&per_page=100&page={page}"
        )
        resp = _safe_get(url, headers=h)
        if resp is None or resp.status_code != 200:
            code = getattr(resp, "status_code", "—")
            print(f"  ⚠  Search Commits API: status {code}")
            break
        data  = resp.json()
        items = data.get("items", [])
        if not items:
            break
        for item in items:
            repo = item["repository"]["full_name"]
            repo_commits[repo] = repo_commits.get(repo, 0) + 1
        total = data.get("total_count", 0)
        print(f"  Página {page}: {len(items)} commits | repos próprios únicos: {len(repo_commits)} | total: {total}")
        if page * 100 >= min(total, 1000):
            break
        time.sleep(0.35)
    return repo_commits


def get_events_third_party() -> dict:
    """
    Busca PushEvents via Events API (últimos 300 eventos).
    Inclui repos de terceiros públicos E privados (quando autenticado com token repo scope).
    Retorna {repo_full_name: commit_count} apenas para repos de terceiros.
    """
    if not GITHUB_TOKEN:
        print("  ⚠  GITHUB_TOKEN não definido — Events API só verá repos públicos.")
        print("     Para incluir repos PRIVADOS de terceiros, defina GITHUB_TOKEN com scope 'repo'.")

    own_prefix = f"{GITHUB_USERNAME.lower()}/"
    third_party: dict = {}

    for page in range(1, 4):  # 3 páginas × 100 = 300 eventos
        url = (
            f"https://api.github.com/users/{GITHUB_USERNAME}/events"
            f"?per_page=100&page={page}"
        )
        resp = _safe_get(url, headers=_gh_headers())
        if resp is None or resp.status_code != 200:
            code = getattr(resp, "status_code", "—")
            print(f"  ⚠  Events API: status {code}")
            break
        batch = resp.json()
        if not batch:
            break
        for ev in batch:
            if ev.get("type") != "PushEvent":
                continue
            repo = ev["repo"]["name"]
            if repo.lower().startswith(own_prefix):
                continue
            n = len(ev["payload"].get("commits", []))
            third_party[repo] = third_party.get(repo, 0) + n
        time.sleep(0.25)

    return third_party


def get_repo_languages(repo_full_name: str) -> dict:
    """Retorna {linguagem: bytes} para um repositório."""
    url  = f"https://api.github.com/repos/{repo_full_name}/languages"
    resp = _safe_get(url, headers=_gh_headers())
    time.sleep(0.2)
    return resp.json() if (resp and resp.status_code == 200) else {}


def get_total_commits() -> int:
    """Conta total de commits do usuário via Search API."""
    url = f"https://api.github.com/search/commits?q=author:{GITHUB_USERNAME}&per_page=1"
    h   = _gh_headers()
    h["Accept"] = "application/vnd.github.cloak-preview+json"
    resp = _safe_get(url, headers=h)
    if resp and resp.status_code == 200:
        return resp.json().get("total_count", 0)
    return 0


def analyze_commits(own_repos: dict, third_party: dict):
    """
    Recebe repos próprios (Search API) e de terceiros (Events API).
    Agrega linguagens (bytes) de TODOS os repos com commits.
    Retorna (third_party dict, lang_bytes Counter).
    """
    all_repos = {**own_repos, **third_party}

    lang_bytes: Counter = Counter()
    total = len(all_repos)
    for i, repo in enumerate(all_repos, 1):
        print(f"  [{i}/{total}] linguagens: {repo}")
        lang_bytes.update(get_repo_languages(repo))

    return third_party, lang_bytes


# ════════════════════════════ README ═════════════════════════════════════════

def _bar(pct: float, width: int = 22) -> str:
    """Barra de progresso em Unicode."""
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def build_readme(
    third_party: dict,
    lang_bytes: Counter,
    ld: dict,
    total_commits: int,
) -> str:
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    # ── Sobre ─────────────────────────────────────────────────────────────
    sobre = ld.get("sobre") or (
        "Engenheiro da Computação com experiência em **Quality Assurance** "
        "(Postman, K6, JMeter). Aprendiz em desenvolvimento Web."
    )

    # ── Formação Acadêmica ─────────────────────────────────────────────────
    formacoes_md = ""
    for item in ld.get("formacoes", []):
        inst  = item.get("instituicao", "")
        curso = item.get("curso", "")
        per   = item.get("periodo", "")
        line  = f"- **{inst}** — {curso}"
        if per:
            line += f" *({per})*"
        formacoes_md += line + "\n"

    # ── Experiência ────────────────────────────────────────────────────────
    exp_md = ""
    for item in ld.get("experiencia", []):
        cargo  = item.get("cargo", "")
        emp    = item.get("empresa", "")
        per    = item.get("periodo", "")
        desc   = item.get("descricao", "")
        line   = f"- **{cargo}** @ {emp}"
        if per:
            line += f" *({per})*"
        if desc:
            line += f"  \n  {desc}"
        exp_md += line + "\n"

    # ── Certificados ──────────────────────────────────────────────────────
    certs_md = ""
    for cert in ld.get("certificados", []):
        nome  = cert.get("nome", "")
        emit  = cert.get("emissor", "")
        data  = cert.get("data", "")
        url   = cert.get("url", "")
        entry = f"[{nome}]({url})" if url else nome
        line  = f"- {entry} — *{emit}*"
        if data:
            line += f" _{data}_"
        certs_md += line + "\n"

    # ── Linguagens ────────────────────────────────────────────────────────
    total_bytes = sum(lang_bytes.values()) or 1
    top_langs   = lang_bytes.most_common(10)
    lang_rows   = ""
    for lang, bcount in top_langs:
        pct       = bcount / total_bytes * 100
        lang_rows += f"| {lang:<20} | {_bar(pct)} | {pct:5.1f}% |\n"

    # ── Repos de terceiros ────────────────────────────────────────────────
    sorted_third = sorted(third_party.items(), key=lambda x: -x[1])[:10]
    third_rows   = ""
    for repo, cnt in sorted_third:
        owner, name = repo.split("/", 1)
        third_rows += f"| [{name}](https://github.com/{repo}) | `{owner}` | {cnt} |\n"

    if not third_rows:
        third_rows = "| *(nenhum no período)* | — | — |\n"

    # ── Contato extra (email / site do LinkedIn) ──────────────────────────
    contato      = ld.get("contato", {})
    extra_badges = ""
    if contato.get("email"):
        extra_badges += (
            f"[![Email](https://img.shields.io/badge/Email-D14836?logo=gmail&logoColor=white)]"
            f"(mailto:{contato['email']})  \n"
        )
    if contato.get("site"):
        extra_badges += (
            f"[![Site](https://img.shields.io/badge/Site-4285F4?logo=googlechrome&logoColor=white)]"
            f"({contato['site']})  \n"
        )

    # ── README final ──────────────────────────────────────────────────────
    readme = f"""# Olá, sou Fabio Chorobura 👋

> *Atualizado automaticamente em {now}*

{sobre}

---

## 🎓 Formação Acadêmica

{formacoes_md.strip() or "_Preencha `linkedin_data.json` com suas formações._"}

---

## � Experiência Profissional

{exp_md.strip() or "_Preencha `linkedin_data.json` com sua experiência._"}

---

## �📜 Cursos & Certificados

{certs_md.strip() or "_Preencha `linkedin_data.json` com seus certificados._"}

---

## 📊 Estatísticas GitHub

<p align="center">
  <img src="https://github-readme-stats.vercel.app/api?username={GITHUB_USERNAME}&show_icons=true&include_all_commits=true&theme=buefy&hide_border=true" alt="GitHub Stats" />
  &nbsp;
  <img src="https://github-readme-stats.vercel.app/api/top-langs/?username={GITHUB_USERNAME}&layout=compact&theme=buefy&hide_border=true" alt="Top Languages" />
</p>

**Total de commits indexados:** {total_commits}

---

## 🔤 Linguagens nos Repositórios com Commits

| Linguagem             | Proporção                |    %   |
|-----------------------|--------------------------|--------|
{lang_rows}
---

## 🤝 Contribuições em Repositórios de Terceiros

| Repositório           | Organização / Dono  | Commits |
|-----------------------|---------------------|---------|
{third_rows}
---

## 📦 Repositórios Destaque

[![JavaScriptEBAC](https://github-readme-stats.vercel.app/api/pin/?username={GITHUB_USERNAME}&repo=JavaScriptEBAC.github.io&theme=buefy&hide_border=true)](https://github.com/{GITHUB_USERNAME}/JavaScriptEBAC.github.io)
[![Portfolio](https://github-readme-stats.vercel.app/api/pin/?username={GITHUB_USERNAME}&repo=portfolio_fc_eng_clean.github.io&theme=buefy&hide_border=true)](https://github.com/{GITHUB_USERNAME}/portfolio_fc_eng_clean.github.io)

---

## 🛠️ Skills

**QA & Testes:**
![Postman](https://img.shields.io/badge/Postman-FF6C37?logo=postman&logoColor=white)
![K6](https://img.shields.io/badge/K6-7D64FF?logo=k6&logoColor=white)
![JMeter](https://img.shields.io/badge/JMeter-D4AF37?logo=java&logoColor=white)

**Desenvolvimento Web:**
![JavaScript](https://img.shields.io/badge/JavaScript-F7DF1E?logo=javascript&logoColor=black)
![HTML5](https://img.shields.io/badge/HTML5-E34C26?logo=html5&logoColor=white)
![CSS3](https://img.shields.io/badge/CSS3-1572B6?logo=css3&logoColor=white)

---

## 📞 Contatos

[![LinkedIn](https://img.shields.io/badge/LinkedIn-0077B5?logo=linkedin&logoColor=white)](https://www.linkedin.com/in/fabiochorobura)
[![GitHub](https://img.shields.io/badge/GitHub-181717?logo=github&logoColor=white)](https://github.com/{GITHUB_USERNAME})
{extra_badges}"""

    return readme.strip() + "\n"


# ════════════════════════════ Main ═══════════════════════════════════════════

def main() -> None:
    print("╔════════════════════════════════════════╗")
    print("║  update_readme.py — fabiochorobura     ║")
    print("╚════════════════════════════════════════╝\n")

    print("▶ [1/4] Carregando dados do LinkedIn …")
    linkedin = load_linkedin_data()

    print("\n▶ [2/4] Buscando commits em repos PRÓPRIOS (Search API — histórico completo) …")
    own_repos = search_own_commits()
    print(f"  {sum(own_repos.values())} commits em {len(own_repos)} repos próprios.")

    print("\n▶ [2b] Buscando commits em repos de TERCEIROS (Events API — inclui privados com token) …")
    third_party_repos = get_events_third_party()
    print(f"  {sum(third_party_repos.values())} commits em {len(third_party_repos)} repos de terceiros.")

    print("\n▶ [3/4] Analisando linguagens dos repos …")
    third_party, lang_bytes = analyze_commits(own_repos, third_party_repos)
    total_commits = get_total_commits()
    print(f"  Repos de terceiros com commits : {len(third_party)}")
    print(f"  Linguagens detectadas          : {len(lang_bytes)}")
    print(f"  Total de commits (Search API)  : {total_commits}")

    print("\n▶ [4/4] Gerando README.md …")
    content = build_readme(third_party, lang_bytes, linkedin, total_commits)
    with open(README_FILE, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"  ✅  README.md atualizado! ({README_FILE})\n")


if __name__ == "__main__":
    main()
