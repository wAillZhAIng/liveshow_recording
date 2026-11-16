"""Small date dropdown for the overview filters."""
from __future__ import annotations

import calendar
import tkinter as tk
from datetime import date
from tkinter import ttk


class DatePicker(ttk.Button):
    def __init__(self, parent, *, variable):
        self.variable = variable
        self.label = tk.StringVar(master=parent)
        self.popup = None
        super().__init__(parent, textvariable=self.label, width=14, command=self.toggle)
        self.trace = self.variable.trace_add("write", self.update_label)
        self.bind("<Destroy>", self.on_destroy, add=True)
        self.update_label()

    def update_label(self, *_args):
        self.label.set(f"{self.variable.get() or '选择日期'} ▾")

    def on_destroy(self, event):
        if event.widget is self:
            self.variable.trace_remove("write", self.trace)

    def toggle(self):
        if self.popup is not None:
            self.close()
            return
        try:
            selected = date.fromisoformat(self.variable.get())
        except ValueError:
            selected = date.today()
        self.month = selected.replace(day=1)
        self.popup = tk.Toplevel(self)
        self.popup.withdraw()
        self.popup.transient(self.winfo_toplevel())
        self.popup.overrideredirect(True)
        self.popup.bind("<Escape>", lambda _event: self.close())
        self.popup.bind("<Button-1>", self.dismiss_outside, add=True)
        self.panel = ttk.Frame(self.popup, padding=8, relief="solid", borderwidth=1)
        self.panel.pack(fill="both", expand=True)
        self.draw_month()
        self.popup.update_idletasks()
        width, height = self.popup.winfo_reqwidth(), self.popup.winfo_reqheight()
        x = min(self.winfo_rootx(), self.winfo_screenwidth() - width)
        y = self.winfo_rooty() + self.winfo_height()
        if y + height > self.winfo_screenheight():
            y = self.winfo_rooty() - height
        self.popup.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.popup.deiconify()
        self.popup.grab_set()
        self.popup.focus_set()

    def dismiss_outside(self, event):
        if event.widget is self.popup:
            x, y = self.popup.winfo_rootx(), self.popup.winfo_rooty()
            if not (x <= event.x_root < x + self.popup.winfo_width()
                    and y <= event.y_root < y + self.popup.winfo_height()):
                self.close()

    def draw_month(self):
        for child in self.panel.winfo_children():
            child.destroy()
        ttk.Button(self.panel, text="‹", width=3, command=lambda: self.change_month(-1)).grid(row=0, column=0)
        ttk.Label(self.panel, text=self.month.strftime("%Y 年 %m 月")).grid(row=0, column=1, columnspan=5)
        ttk.Button(self.panel, text="›", width=3, command=lambda: self.change_month(1)).grid(row=0, column=6)
        for column, weekday in enumerate("一二三四五六日"):
            ttk.Label(self.panel, text=weekday).grid(row=1, column=column)
        for row, week in enumerate(calendar.Calendar().monthdayscalendar(self.month.year, self.month.month), 2):
            for column, day in enumerate(week):
                if day:
                    value = self.month.replace(day=day).isoformat()
                    ttk.Button(self.panel, text=str(day), width=3,
                               command=lambda value=value: self.choose(value)).grid(row=row, column=column)
        ttk.Button(self.panel, text="今天", command=lambda: self.choose(date.today().isoformat())).grid(row=8, column=0, columnspan=3)
        ttk.Button(self.panel, text="清空", command=lambda: self.choose("")).grid(row=8, column=4, columnspan=3)

    def change_month(self, step):
        year, month = divmod(self.month.year * 12 + self.month.month - 1 + step, 12)
        if 1 <= year <= 9999:
            self.month = date(year, month + 1, 1)
            self.draw_month()

    def choose(self, value):
        self.variable.set(value)
        self.close()

    def close(self):
        if self.popup is not None:
            self.popup.destroy()
            self.popup = None
            self.focus_set()
