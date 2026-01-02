from lark import Lark

GRAMMAR_PATH = "nist_gc.lark"

class NistParser:
    def __init__(self):
        self.parser = Lark.open(GRAMMAR_PATH, start="start", parser="lalr", debug=True)

    def parse(self, input_path):
        with open(input_path, "r") as f:
            text = f.read()

        if not text.endswith("\n"):
            text = text + "\n"

        parse_tree = self.parser.parse(text)
        return parse_tree
