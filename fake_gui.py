"""
Flet GUI sample.

Run with:
    pip install flet
    python fake_gui.py
"""

import asyncio
import time
import flet as ft


async def main(page: ft.Page):
    page.title = "Flet Sample"
    page.window.width = 520
    page.window.height = 420
    page.padding = 20

    # --- 1. FilePicker — now a service in Flet 1.0, added to page.services ---
    file_picker = ft.FilePicker()
    page.services.append(file_picker)

    csv_path_field = ft.TextField(
        label="CSV File Path",
        hint_text="e.g. /path/to/data.csv",
        expand=True,
    )

    async def on_browse(e):
        result = await file_picker.pick_files_async(
            allowed_extensions=["csv"],
            allow_multiple=False,
        )
        if result and result.files:
            csv_path_field.value = result.files[0].path
            page.update()

    browse_button = ft.ElevatedButton(
        "Browse",
        icon=ft.Icons.FOLDER_OPEN,
        on_click=on_browse,
    )

    # --- 2. Dropdown with 2 values ---
    mode_dropdown = ft.Dropdown(
        label="Processing Mode",
        width=460,
        value="fast",
        options=[
            ft.dropdown.Option("fast", "Fast Mode"),
            ft.dropdown.Option("accurate", "Accurate Mode"),
        ],
    )

    # --- 3. Numeric input (batch size) ---
    batch_size_field = ft.TextField(
        label="Batch Size",
        value="100",
        width=460,
        keyboard_type=ft.KeyboardType.NUMBER,
        input_filter=ft.NumbersOnlyInputFilter(),
    )

    # --- 4. Progress bar ---
    progress_bar = ft.ProgressBar(width=460, value=0)
    status_text = ft.Text("Ready")

    async def run_process(e):
        if not csv_path_field.value:
            status_text.value = "Please select a CSV file first."
            page.update()
            return

        try:
            batch_size = int(batch_size_field.value)
            if batch_size <= 0:
                raise ValueError
        except (TypeError, ValueError):
            status_text.value = "Batch size must be a positive integer."
            page.update()
            return

        status_text.value = (
            f"Processing '{csv_path_field.value}' in {mode_dropdown.value} mode "
            f"(batch size = {batch_size})..."
        )
        run_button.disabled = True
        progress_bar.value = 0
        page.update()

        for i in range(batch_size + 1):
            progress_bar.value = i / batch_size
            page.update()
            await asyncio.sleep(0.02)

        status_text.value = "Done!"
        run_button.disabled = False
        page.update()

    run_button = ft.ElevatedButton(
        "Run",
        icon=ft.Icons.PLAY_ARROW,
        on_click=run_process,
    )

    page.add(
        ft.Text("CSV Processor", size=22, weight=ft.FontWeight.BOLD),
        ft.Row([csv_path_field, browse_button]),
        mode_dropdown,
        batch_size_field,
        ft.Divider(),
        status_text,
        progress_bar,
        run_button,
    )


if __name__ == "__main__":
    ft.app(target=main)
