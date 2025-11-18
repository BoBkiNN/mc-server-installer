import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Generic, TypeVar
from zipfile import ZipFile

from asteval import Interpreter
from asteval.astutils import ExceptionHolder
from core import AssetsGroup, DownloadData, Environment
from model import (Asset, BaseAction, DummyAction, Expr, RenameFile,
                   TemplateExpr, UnzipFile)


class ExpressionProcessor:
    def __init__(self, logger: logging.Logger, folder: Path, env: Environment) -> None:
        self.logger = logger
        self.folder = folder
        self.env = env
        self.intpr = Interpreter(minimal=True)
        self.setsym(self.env, True, "env")
        self.setsym(self.env.profile, True, "profile")

    def log_error(self, error: ExceptionHolder, expr: Expr, source_key: str, source_text: str):
        if not error.exc:
            exc_name = "UnknownError"
        else:
            try:
                exc_name = error.exc.__name__
            except AttributeError:
                exc_name = str(error.exc)
            if exc_name in (None, 'None'):
                exc_name = "UnknownError"
        exc_msg = str(error.msg)

        lineno = getattr(error.node, "lineno", 1) or 1
        col = getattr(error.node, "col_offset", 0) or 0

        # Extract offending line from source_text
        src_lines = source_text.splitlines()
        line_text = src_lines[lineno - 1] if 0 <= lineno - \
            1 < len(src_lines) else ""

        # Build caret marker
        marker = " " * col + "^"

        # Final pretty message
        msg = (
            f"💥 Failed to evaluate expression in {source_key}\n"
            f"  Expression: {str(expr)!r}\n"
            f"  {exc_name}: {exc_msg}\n"
            f"  {line_text}\n"
            f"  {marker} (line {lineno}, column {col})"
        )
        self.logger.error(msg)

    def eval(self, expr: Expr, source_key: str, source_text: str):
        res = self.intpr.eval(expr)
        errors: list[ExceptionHolder] = self.intpr.error
        error = errors[0] if errors else None
        if error is None:
            return res
        self.log_error(error, expr, source_key, source_text)
        return error

    def eval_template(self, expr: TemplateExpr, source_key: str, source_text: str):
        parts = expr.parts()
        bs = ""
        ei = 0
        for part in parts:
            if isinstance(part, Expr):
                v = self.eval(part, source_key+f"${ei}", source_text)
                if isinstance(v, ExceptionHolder):
                    return v
                bs += str(v)
                ei += 1
            else:
                bs += part
        return bs

    def eval_if(self, key: str, if_code: Expr):
        """
        Executes a code string and returns True or False.

        Integer return values are converted to boolean using `bool(v)`.

        :param key: Key to description
        :type key: str
        :param if_code: Code to evaluate
        :type if_code: Expr

        :return: True if code returned True or any truthy value, False if code returned False, None if evaluation error occurred
        :rtype: bool or None
        """
        v = self.eval(if_code, key, str(if_code))
        if isinstance(v, ExceptionHolder):
            self.logger.error(
                "Failed to process if statement, see above errors for details")
            return
        if isinstance(v, bool):
            b = v
        elif isinstance(v, int):
            b = bool(v)
        elif isinstance(v, str):
            b = True if v.lower() == "true" else False
        else:
            self.logger.warning(
                f"If statement in {key} returned non-bool. Expected True of False")
            b = True
        return b

    def handle(self, key: str, action: BaseAction, data: DownloadData):
        # TODO return bool or enum stating error or ok
        if_code = action.if_
        if if_code:
            b = self.eval_if(key+".if", if_code)
            if not b:  # False or None
                return
        action_type = action.get_type()
        handler = self.env.registries.get_entry(ActionHandler, action_type)
        if handler is None:
            raise ValueError(
                f"Failed to find provider for action {action_type!r}")
        handler.handle(self, key, action, data)

    def setsym(self, value: Any, const: bool, *names: str):
        for name in names:
            self.intpr.symtable[name] = value
            if const:
                self.intpr.readonly_symbols.add(name)

    def process(self, asset: Asset, group: "AssetsGroup", data: DownloadData):
        ls = asset.actions
        if not ls:
            return data
        self.setsym(data, True, "data", "d")
        self.setsym(data, True, "asset", "a")
        ak = group.get_manifest_name()+"."+asset.resolve_asset_id()
        for i, a in enumerate(ls):
            key = f"{ak}.actions[{i}]"
            try:
                self.handle(key, a, data)
            except Exception as e:
                self.logger.error(
                    f"Failed to handle action {type(a)} at {key}", exc_info=e)


ACTION = TypeVar("ACTION", bound=BaseAction)


class ActionHandler(ABC, Generic[ACTION]):
    @abstractmethod
    def handle(self, proc: ExpressionProcessor, key: str, action: ACTION, data: DownloadData) -> bool:
        ...


class DummyActionHandler(ActionHandler[DummyAction]):
    def handle(self, proc: ExpressionProcessor, key: str, action: DummyAction, data: DownloadData) -> bool:
        v = proc.eval(action.expr, key+".expr", str(action.expr))
        if isinstance(v, ExceptionHolder):
            proc.logger.error(
                "Failed to process expression, see above errors for details")
            return False
        proc.logger.info(f"Dummy expression at {key} returned {v}")
        return True


class RenameActionHandler(ActionHandler[RenameFile]):
    def handle(self, proc: ExpressionProcessor, key: str, action: RenameFile, data: DownloadData) -> bool:
        frp = data.primary
        if not frp:
            proc.logger.error("No files to rename")
            return False
        to = proc.eval_template(action.to, key+".to", str(action.to))
        if isinstance(to, ExceptionHolder):
            return False
        top = frp.with_name(to)
        if top.is_file():
            os.remove((proc.folder / top).resolve())
        frp.rename(top)
        data.primary = top
        proc.logger.info(f"✅ Renamed file from {frp} to {top}")
        return True


class UnzipActionHandler(ActionHandler[UnzipFile]):
    def handle(self, proc: ExpressionProcessor, key: str, action: UnzipFile, data: DownloadData) -> bool:
        if action.folder.root:
            folder = proc.eval_template(
                action.folder, key+".folder", action.folder.root)
            if isinstance(folder, ExceptionHolder):
                return False
        else:
            pf = data.primary.parent
            if pf.is_absolute():
                folder = pf
            else:
                folder = proc.folder / data.primary.parent
        with ZipFile(proc.folder / data.primary, "r") as zip_ref:
            zip_ref.extractall(folder)
        proc.logger.info(f"✅ Unzipped {data.primary} into {folder}")
        return True
