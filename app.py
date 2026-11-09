"""Windows desktop widget for recent quantum-computing and AI world-model news."""

from __future__ import annotations

import argparse
from datetime import datetime
import os
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox
from tkinter import ttk
from typing import Any

from news import (
    Article,
    DEFAULT_CATEGORY,
    NEWS_CATEGORIES,
    NewsError,
    fetch_article_bodies,
    fetch_articles,
    summarize_from_feed,
    summarize_with_ai,
)

POLL_INTERVAL_MS = 100


def _scheduled_marker_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base_directory = (
        Path(local_app_data)
        if local_app_data
        else Path.home() / "AppData" / "Local"
    )
    return base_directory / "VeilleActualites" / "last-opened.txt"


def _legacy_scheduled_marker_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base_directory = (
        Path(local_app_data)
        if local_app_data
        else Path.home() / "AppData" / "Local"
    )
    return base_directory / "ActualitesBiologieQuantique" / "last-opened.txt"


def _already_opened_today() -> bool:
    marker = _scheduled_marker_path()
    try:
        last_opened = marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        try:
            last_opened = _legacy_scheduled_marker_path().read_text(
                encoding="utf-8"
            ).strip()
        except FileNotFoundError:
            return False
    return last_opened == datetime.now().astimezone().date().isoformat()


def _mark_opened_today() -> None:
    marker = _scheduled_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        datetime.now().astimezone().date().isoformat(),
        encoding="utf-8",
    )


def scheduled_delay_ms(now: datetime) -> int:
    """Return the time remaining until 11:00 local time, or zero if past."""
    opening_time = now.replace(hour=11, minute=0, second=0, microsecond=0)
    return max(0, int((opening_time - now).total_seconds() * 1000))


class NewsWidget:
    def __init__(
        self,
        root: tk.Tk,
        *,
        auto_refresh: bool = True,
        show_loading_on_start: bool = False,
    ) -> None:
        self.root = root
        self.window_visible = False
        self.results: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.root.title("Veille — Informatique quantique et IA")
        self.root.geometry("760x720")
        self.root.minsize(560, 420)
        self.root.configure(bg="#f4f6fa")

        style = ttk.Style()
        style.configure("TFrame", background="#f4f6fa")
        style.configure("Title.TLabel", background="#f4f6fa", font=("Segoe UI", 17, "bold"))
        style.configure("Muted.TLabel", background="#f4f6fa", foreground="#566174")
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 7))

        header = ttk.Frame(root, padding=(20, 18, 20, 12))
        header.pack(fill=tk.X)
        ttk.Label(
            header,
            text="Veille scientifique",
            style="Title.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            header,
            text="Actualités récentes en informatique quantique et modèles du monde en IA",
            style="Muted.TLabel",
        ).pack(anchor=tk.W, pady=(4, 12))

        controls = ttk.Frame(header)
        controls.pack(fill=tk.X)
        self.refresh_button = ttk.Button(
            controls, text="Actualiser", command=self.refresh
        )
        self.refresh_button.pack(side=tk.LEFT)
        self.status = ttk.Label(controls, text="Prêt", style="Muted.TLabel")
        self.status.pack(side=tk.LEFT, padx=12)
        self.loading_indicator = ttk.Progressbar(
            controls,
            mode="indeterminate",
            length=120,
        )

        content = ttk.Frame(root, padding=(20, 0, 20, 18))
        content.pack(fill=tk.BOTH, expand=True)
        self.notebook = ttk.Notebook(content)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self.reports: dict[str, tk.Text] = {}
        for category, config in NEWS_CATEGORIES.items():
            tab = ttk.Frame(self.notebook, padding=(8, 10))
            self.notebook.add(tab, text=config.title)
            report = tk.Text(
                tab,
                wrap=tk.WORD,
                relief=tk.FLAT,
                bg="white",
                fg="#172033",
                font=("Segoe UI", 10),
                padx=16,
                pady=14,
                spacing1=3,
                spacing3=8,
                cursor="arrow",
                state=tk.DISABLED,
            )
            scrollbar = ttk.Scrollbar(
                tab,
                orient=tk.VERTICAL,
                command=report.yview,
            )
            report.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            report.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            report.tag_configure(
                "heading", font=("Segoe UI", 12, "bold"), spacing1=12
            )
            report.tag_configure(
                "link", foreground="#0759a5", underline=True, spacing1=3
            )
            self.reports[category] = report

        self._write(
            "La veille quotidienne s’ouvre à 11 h lorsque votre session "
            "Windows est connectée.\n\n"
            "Cliquez sur « Actualiser » pour rechercher les actualités des "
            "deux rubriques.",
            DEFAULT_CATEGORY,
        )
        for category in NEWS_CATEGORIES:
            if category != DEFAULT_CATEGORY:
                self._write(
                    "Cliquez sur « Actualiser » pour rechercher les actualités.",
                    category,
                )
        self.root.after(POLL_INTERVAL_MS, self._poll_results)
        if auto_refresh:
            self.refresh()
        if show_loading_on_start:
            self.root.deiconify()
            self.window_visible = True

    def _write(self, text: str, category: str = DEFAULT_CATEGORY) -> None:
        report = self.reports[category]
        report.configure(state=tk.NORMAL)
        report.delete("1.0", tk.END)
        report.insert(tk.END, text)
        report.configure(state=tk.DISABLED)

    def refresh(self) -> None:
        if str(self.refresh_button["state"]) == tk.DISABLED:
            return
        self.refresh_button.configure(state=tk.DISABLED)
        self.status.configure(text="Recherche des nouvelles…")
        self.loading_indicator.pack(side=tk.RIGHT, padx=(8, 0))
        self.loading_indicator.start(12)
        threading.Thread(target=self._load_news, daemon=True).start()

    def _load_news(self) -> None:
        category_results = []
        for category in NEWS_CATEGORIES:
            try:
                articles, warnings = fetch_articles(category)
            except NewsError as exc:
                category_results.append(
                    (category, [], [], [f"Recherche impossible : {exc}"])
                )
                continue

            articles, body_warnings = fetch_article_bodies(articles)
            warnings.extend(body_warnings)
            try:
                points = summarize_with_ai(articles)
            except NewsError as exc:
                points = [
                    {
                        **point,
                        "summary": (
                            "Résumé en français indisponible; extrait original : "
                            f"{point['summary']}"
                        ),
                    }
                    for point in summarize_from_feed(articles)
                ]
                warnings.append(
                    f"Résumé IA indisponible : {exc} "
                    "Les extraits de secours peuvent rester dans la langue source."
                )
            category_results.append((category, articles, points, warnings))

        self.results.put(("success", category_results))

    def _poll_results(self) -> None:
        try:
            result_type, payload = self.results.get_nowait()
        except queue.Empty:
            self.root.after(POLL_INTERVAL_MS, self._poll_results)
            return

        self.refresh_button.configure(state=tk.NORMAL)
        self.loading_indicator.stop()
        self.loading_indicator.pack_forget()
        if result_type == "error":
            self.status.configure(text="Échec de la recherche")
            self._write(f"Impossible de récupérer les nouvelles.\n\n{payload}")
        else:
            article_count = 0
            warning_count = 0
            for category, articles, points, warnings in payload:
                self._display_report(category, articles, points, warnings)
                article_count += len(articles)
                warning_count += len(warnings)
            suffix = f" — {warning_count} avertissement(s)" if warning_count else ""
            self.status.configure(
                text=f"{article_count} nouvelle(s) — mis à jour{suffix}"
            )
        if not self.window_visible:
            self.root.deiconify()
            self.window_visible = True
        self.root.after(POLL_INTERVAL_MS, self._poll_results)

    def _display_report(
        self,
        category: str,
        articles: list[Article],
        points: list[dict[str, Any]],
        warnings: list[str],
    ) -> None:
        report = self.reports[category]
        report.configure(state=tk.NORMAL)
        report.delete("1.0", tk.END)
        report.insert(tk.END, f"{NEWS_CATEGORIES[category].title}\n", "heading")
        summaries = {
            point["article_index"]: point for point in points
        }
        for index, article in enumerate(articles, start=1):
            report.insert(tk.END, f"{index}. ")
            self._insert_link(report, article.title, article.url)
            details = f"\n   {article.source} · {article.display_date}"
            point = summaries.get(index)
            if point:
                details += f"\n   Résumé par : {point.get('provider', 'IA non précisée')}"
                details += f"\n   {point['summary']}"
            elif article.description:
                details += f"\n   {article.description}"
            report.insert(tk.END, f"{details}\n\n")

        if not articles:
            report.insert(tk.END, "Aucune nouvelle récente disponible.\n\n")
        if warnings:
            report.insert(tk.END, "Avertissements\n", "heading")
            for warning in warnings:
                report.insert(tk.END, f"• {warning}\n\n")

        report.configure(state=tk.DISABLED)
        report.see("1.0")

    def _insert_link(self, report: tk.Text, text: str, url: str) -> None:
        tag = f"link_{report.index(tk.END).replace('.', '_')}"
        report.insert(tk.END, text, ("link", tag))
        report.tag_bind(
            tag, "<Button-1>", lambda _event, target=url: webbrowser.open(target)
        )
        report.tag_bind(tag, "<Enter>", lambda _event: report.configure(cursor="hand2"))
        report.tag_bind(tag, "<Leave>", lambda _event: report.configure(cursor="arrow"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Veille quotidienne d’actualités.")
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    root = tk.Tk()
    root.withdraw()
    if not args.scheduled:
        NewsWidget(root, show_loading_on_start=True)
    else:
        try:
            already_opened = _already_opened_today()
        except OSError as exc:
            root.withdraw()
            messagebox.showerror(
                "Planification",
                f"Impossible de vérifier la dernière ouverture automatique : {exc}",
                parent=root,
            )
            root.destroy()
            return
        if already_opened:
            root.destroy()
            return

        widget = NewsWidget(root, auto_refresh=False)

        def open_at_schedule() -> None:
            try:
                already_opened = _already_opened_today()
            except OSError as exc:
                root.deiconify()
                messagebox.showerror(
                    "Planification",
                    f"Impossible de vérifier la dernière ouverture automatique : {exc}",
                    parent=root,
                )
                root.destroy()
                return
            if already_opened:
                root.destroy()
                return
            try:
                _mark_opened_today()
            except OSError as exc:
                messagebox.showwarning(
                    "Planification",
                    f"Impossible d’enregistrer l’ouverture du widget : {exc}",
                    parent=root,
                )
            else:
                widget.refresh()

        root.after(scheduled_delay_ms(datetime.now().astimezone()), open_at_schedule)

    root.mainloop()


if __name__ == "__main__":
    main()
