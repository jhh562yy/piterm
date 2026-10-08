"""Expression utilities: validate, evaluate (scalar), search functions."""

from __future__ import annotations

from qgis.core import QgsExpression, QgsExpressionContext

from .. import compat
from ..errors import bad_request, invalid, not_found
from ..registry import command
from .base import HandlerBase, value_to_json


class ExpressionHandlers(HandlerBase):

    @command(name="expression.validate", summary="Parse/validate a QGIS "
             "expression and list referenced columns", risk="read",
             tags=("expression",))
    def expression_validate(self, expression, layer=None, sample=False):
        expr = QgsExpression(str(expression))
        out = {"expression": str(expression),
               "valid": not expr.hasParserError(),
               "parser_error": expr.parserErrorString() or None}
        try:
            out["referenced_columns"] = list(expr.referencedColumns())
        except Exception:  # noqa: BLE001
            out["referenced_columns"] = []
        try:
            out["referenced_functions"] = list(expr.referencedFunctions())
        except Exception:  # noqa: BLE001
            pass
        try:
            out["needs_geometry"] = bool(expr.needsGeometry())
        except Exception:  # noqa: BLE001
            pass
        if layer is not None:
            target = self.resolve_vector({"layer": layer})
            context = self.expression_context(target)
            if expr.prepare(context):
                out["prepared"] = True
                if sample:
                    samples = []
                    for feature in target.getFeatures():
                        context.setFeature(feature)
                        value = expr.evaluate(context)
                        samples.append({"fid": feature.id(),
                                        "value": value_to_json(value),
                                        "error": expr.evalErrorString() or None})
                        if len(samples) >= 3:
                            break
                    out["sample"] = samples
            else:
                out["prepared"] = False
                out["eval_error"] = expr.evalErrorString() or "prepare() failed"
        return out

    @command(name="expression.evaluate", summary="Evaluate an expression to a "
             "scalar (project scope, or a feature context)", risk="read",
             tags=("expression",))
    def expression_evaluate(self, expression, layer=None, fid=None,
                            values=None):
        expr = QgsExpression(str(expression))
        if expr.hasParserError():
            raise invalid("expression parse error: %s" % expr.parserErrorString(),
                          detail={"parser_error": expr.parserErrorString()})
        if layer is not None:
            target = self.resolve_vector({"layer": layer})
            context = self.expression_context(target)
            if fid is not None:
                feature = target.getFeature(int(fid))
                if not feature.isValid():
                    raise not_found("no feature %s" % fid)
                context.setFeature(feature)
            if not expr.prepare(context):
                raise invalid("expression cannot be evaluated: %s"
                              % (expr.evalErrorString() or "unknown"))
            value = expr.evaluate(context)
        else:
            from qgis.core import QgsExpressionContextUtils
            context = QgsExpressionContext()
            context.appendScope(QgsExpressionContextUtils.projectScope(self.project))
            value = expr.evaluate(context) if not expr.hasEvalError() else None
        if expr.hasEvalError():
            raise invalid("evaluation error: %s" % expr.evalErrorString())
        return {"expression": str(expression), "value": value_to_json(value),
                "type": type(value).__name__}

    @command(name="expression.functions_search", summary="Search built-in "
             "expression functions", risk="read", tags=("expression",))
    def expression_functions_search(self, query=None, limit=40):
        functions = []
        try:
            functions = list(QgsExpression.Functions())
        except Exception:  # noqa: BLE001
            try:
                functions = list(QgsExpression.Functions)
            except Exception:  # noqa: BLE001
                functions = []
        text = str(query).casefold() if query else None
        out = []
        for fn in functions:
            try:
                name = fn.name()
            except Exception:  # noqa: BLE001
                continue
            if text and text not in name.casefold():
                continue
            entry = {"name": name}
            for attr in ("description", "helpText"):
                getter = getattr(fn, attr, None)
                if callable(getter):
                    try:
                        value = getter()
                        if value:
                            entry["description"] = str(value)[:300]
                            break
                    except Exception:  # noqa: BLE001
                        continue
            out.append(entry)
            if len(out) >= max(1, min(int(limit), 400)):
                break
        return {"count": len(out), "functions": out}
