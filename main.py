from nist_parser import NistParser
from nist_transformer import NistTransformer
from nist_interpreter import NistInterpreter

INPUT_PATH = "input.gcode"

if __name__ == "__main__":
    parser = NistParser()
    parse_tree = parser.parse(input_path=INPUT_PATH)

    print(f"Result: {parse_tree.pretty()}")

    #transformer = NistTransformer()
    #ast_tree = transformer.transform(parse_tree)

    #print(f"Parsed blocks (compact): {ast_tree}")

    #interpreter = NistInterpreter()
    #ir = interpreter.interpret(ast_tree)
    #for item in ir:
        #print(item)



