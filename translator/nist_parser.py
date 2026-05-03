from lark import Lark

GRAMMAR_PATH = "translator/nist_gc.lark"

class NistParser:
    def __init__(self):
        self.parser = Lark.open(
            GRAMMAR_PATH,
            start="start",
            parser="lalr",
            debug=True
        )

    def preprocess_percent_delimiters(self, text):
        lines = text.splitlines(keepends=True)  # splits text into a list of lines, while preserving the line break character at the end of each line

        # remove blank lines from the beginning
        first_nonblank_index = None
        for i, line in enumerate(lines):
            if line.strip():
                first_nonblank_index = i
                break

        if first_nonblank_index is None:
            return "\n"

        first_line = lines[first_nonblank_index].strip()

        # case 1: file starts with %
        if first_line == "%":
            body_start = first_nonblank_index + 1

            end_index = None
            for i in range(body_start, len(lines)):
                if lines[i].strip() == "%":
                    end_index = i
                    break

            if end_index is None:
                raise SyntaxError("File starts with %, but no closing % was found.")

            # ignore everything after second %
            return "".join(lines[body_start:end_index]) # does not include line end_index

        # case 2: no starting %, full file is parsed
        return text

    def parse(self, input_path):
        with open(input_path, "r", encoding="utf-8") as f:
            text = f.read()

        text = self.preprocess_percent_delimiters(text)

        if not text.endswith("\n"): # file must end with a "\n" so lark doesn't fail, could modify the grammar but i choose to keep it as close as I can to documentation
            text = text + "\n"

        parse_tree = self.parser.parse(text)
        return parse_tree
