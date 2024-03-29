import os
import os.path as osp
import ast
import ast
import black
import astunparse
import textwrap
from typing import Tuple


def create_dummy_function(body) -> ast.FunctionDef:
    """
    Create a dummy ast.FunctionDef object with
    name = "main" and body = body
    """
    func_args = ast.arguments(
        posonlyargs=[],
        args=[],
        vararg=None,
        kwonlyargs=[],
        kw_defaults=[],
        kwarg=None,
        defaults=[],
    )

    func = ast.FunctionDef(
        name="main",
        args=func_args,
        body=body,
        decorator_list=[],
        returns=None,
        type_comment=None,
    )

    return func


def normalize_code_str(code: str) -> str:
    """
    Returns a formatting-normalized version of the code by running through
    a parser and then a code-generator.
    Args:
        code: A string corresponding to the code to normalize
    Returns:
        (str): The normalized code.
    """
    return normalize_code_ast(ast.parse(code))


def normalize_code_ast(code_ast: ast.AST) -> str:
    """
    Returns a formatting-normalized version of the provided Python AST by
    running through a code-generator.
    Args:
        code_ast: The Python AST to unparse.
    Returns:
        (str): A normalized code string.
    """
    mode = black.FileMode()
    result = black.format_str(astunparse.unparse(code_ast).strip(), mode=mode)
    return result.strip()


def is_library_used(filepath: str, lib: str) -> bool:
    """Check if a library is used in a python file.
    (Note: this is a quick and dirty check; A more precise check
    can be done by using the AST of each file => #TODO)

    Args:
        filepath (str): path to the .py file
        lib (str): library name (e.g. "numpy")

    Returns:
        bool: True if the library is used in the file
    """
    keywords = [f"import {lib}", f"from {lib}"]

    with open(filepath, encoding="utf8", errors="ignore") as fp:
        text = fp.read()

        if any(usage in text for usage in keywords):
            return True

    return False


def save_example(code_str: str, path: str):
    """save a code example to disk"""
    with open(path, "w") as fp:
        fp.write(code_str)


def breakdown_code_methods(outdir: str, path: str, file_id: str) -> Tuple[int, list]:
    """Breakdown a python file into methods.
    Save the methods into seperate files.

    Args:
        path (str): path to the .py file

    Returns:
        int: number of methods found
        list: list of methods created
    """
    methods_created = []
    example_id = 0
    code = None
    with open(path, "r", encoding="utf-8") as fp:
        try:
            source = fp.read()
            code = ast.parse(source, mode="exec")
        except:
            return 0, []

    # process methods
    classes = [n for n in code.body if isinstance(n, ast.ClassDef)]

    if len(classes) > 0:
        for class_ in classes:
            methods = [n for n in class_.body if isinstance(n, ast.FunctionDef)]

            for meth in methods:
                if meth.name.startswith("test"):
                    continue
                try:
                    example_name = "{}_{}.py".format(file_id, example_id)
                    code_str = astunparse.unparse(meth)
                    save_example(code_str, osp.join(outdir, example_name))
                    methods_created.append(example_name)
                    example_id += 1
                except RecursionError:
                    os.remove(osp.join(outdir, example_name))
                    continue

    # process functions
    functions = [n for n in code.body if isinstance(n, ast.FunctionDef)]

    if len(functions) > 0:
        for func in functions:
            if func.name.startswith("test"):
                continue
            try:
                example_name = "{}_{}.py".format(file_id, example_id)
                code_str = astunparse.unparse(func)
                save_example(code_str, osp.join(outdir, example_name))
                methods_created.append(example_name)
                example_id += 1
            except RecursionError:
                os.remove(osp.join(outdir, example_name))
                continue

    return example_id, methods_created


class ASTMethodDropper(ast.NodeTransformer):
    def visit_Import(self, node: ast.Import):
        return None

    def visit_ImportFrom(self, node: ast.ImportFrom):
        return None

    def visit_ClassDef(self, node: ast.ClassDef):
        super().generic_visit(node)
        return None

    def visit_FunctionDef(self, node: ast.FunctionDef):
        super().generic_visit(node)
        return None


class CodeSpan(ast.NodeTransformer):
    def __init__(self, source):
        self.source = source
        self.lines = source.split("\n")

    def _get_char_index(self, lineno, col_offset):
        line_index = lineno - 1
        line_start = sum(len(line) + 1 for line in self.lines[:line_index])
        return line_start + col_offset

    def _add_span(self, node):
        try:
            lineno = node.lineno
            end_lineno = node.end_lineno
            col_offset = node.col_offset
            end_col_offset = node.end_col_offset

            span_start = self._get_char_index(lineno, col_offset)
            span_end = self._get_char_index(end_lineno, end_col_offset)
            node.span = self.source[span_start:span_end].strip()
        except:
            node.span = ""

        return node

    def visit(self, node):
        """Visit a node."""
        method = "visit_" + node.__class__.__name__
        visitor = getattr(self, method, self.generic_visit)
        return visitor(node)

    def generic_visit(self, node):
        """Called if no explicit visitor function exists for a node."""
        self._add_span(node)

        for key, value in ast.iter_fields(node):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        self._add_span(item)
                        self.visit(item)

            elif isinstance(value, ast.AST):
                self._add_span(value)
                self.visit(value)

        return node
