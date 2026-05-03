from translator.nist_parser import NistParser
from translator.nist_transformer import NistTransformer
from translator.nist_interpreter import NistInterpreter
from translator.hpgl_postprocessor import HPGLPostProcessor

INPUT_PATH = "sample_gcode/sample_c1.gcode"

if __name__ == "__main__":
    parser = NistParser()
    parse_tree = parser.parse(input_path=INPUT_PATH)

    #print(f"Result: {parse_tree.pretty()}")

    transformer = NistTransformer()
    ast_tree = transformer.transform(parse_tree)

    #print(f"Parsed blocks (compact): {ast_tree}")
    #print(json.dumps(asdict(ast_tree), indent=2))

    interpreter = NistInterpreter()
    ir = interpreter.interpret(ast_tree)
    #for item in ir:
        #print(item)

    post = HPGLPostProcessor()
    hpgl = post.translate(ir)

    print(hpgl)



