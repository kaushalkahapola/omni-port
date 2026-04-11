import re

def _has_undeclared_variables(file_content: str, old_string: str, new_string: str):
    tokens = set(re.findall(r'\b[a-z][a-zA-Z0-9_]*\b', new_string))
    old_tokens = set(re.findall(r'\b[a-z][a-zA-Z0-9_]*\b', old_string))
    introduced_tokens = tokens - old_tokens
    
    java_keywords = {"abstract", "assert", "boolean", "break", "byte", "case", "catch", "char",
        "class", "const", "continue", "default", "do", "double", "else", "enum",
        "extends", "final", "finally", "float", "for", "goto", "if", "implements",
        "import", "instanceof", "int", "interface", "long", "native", "new",
        "package", "private", "protected", "public", "return", "short", "static",
        "strictfp", "super", "switch", "synchronized", "this", "throw", "throws",
        "transient", "try", "void", "volatile", "while", "true", "false", "null",
        "var", "record", "sealed", "permits", "yield"}
        
    to_check = {t for t in introduced_tokens if len(t) > 2 and t not in java_keywords}
    
    surrounding_context = file_content.replace(old_string, "", 1)
    surrounding_tokens = set(re.findall(r'\b[a-z][a-zA-Z0-9_]*\b', surrounding_context))
    
    missing_from_file = to_check - surrounding_tokens
    
    for token in missing_from_file:
        decl_pattern = rf'(?:\w+(?:<.*>)?(?:\[\])*)\s+{token}\s*(?:[=;),])'
        if not re.search(decl_pattern, new_string):
            return True, token
            
    return False, ""

file_content = open("repos/crate/server/src/main/java/io/crate/expression/scalar/ArrayUpperFunction.java").read()
import json
res = json.load(open("tests/shadow_run_results_v3/crate/TYPE-V_3dea6f6f/results.json"))
hunk = res["summary"]["failed_hunks"][0]
print(_has_undeclared_variables(file_content, hunk["old_content"], hunk["new_content"]))
