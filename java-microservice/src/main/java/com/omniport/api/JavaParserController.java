package com.omniport.api;

import com.omniport.ast.JavaParserService;
import org.springframework.context.annotation.Lazy;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/javaparser")
@Lazy
public class JavaParserController {

    private JavaParserService javaParserService;

    private JavaParserService getService() {
        if (javaParserService == null) {
            javaParserService = new JavaParserService();
        }
        return javaParserService;
    }

    @PostMapping("/resolve")
    public Map<String, Object> resolveSymbols(@RequestBody Map<String, Object> request) {
        String repoPath = (String) request.get("repo_path");
        String filePath = (String) request.get("file_path");
        @SuppressWarnings("unchecked")
        List<String> symbols = (List<String>) request.get("symbols_to_resolve");
        return getService().resolveSymbols(repoPath, filePath, symbols);
    }

    @PostMapping("/find-method")
    public Map<String, Object> findMethod(@RequestBody Map<String, Object> request) {
        String repoPath = (String) request.get("repo_path");
        String sourceFilePath = (String) request.get("source_file_path");
        @SuppressWarnings("unchecked")
        List<String> methodNames = (List<String>) request.get("method_names");
        return getService().findMethodDefinitions(repoPath, sourceFilePath, methodNames);
    }

    /**
     * Return modifier information for methods in the given file.
     *
     * Request body:
     *   repo_path    — absolute path to the repository root
     *   file_path    — path to the Java file (relative to repo_path)
     *   method_names — list of method names to inspect
     *
     * Response body:
     *   status  → "ok" | "error"
     *   methods → map of methodName → {visibility, modifiers, is_abstract, has_body,
     *                                    is_class_abstract, declaring_class}
     */
    @PostMapping("/method-modifiers")
    public Map<String, Object> methodModifiers(@RequestBody Map<String, Object> request) {
        String repoPath = (String) request.get("repo_path");
        String filePath = (String) request.get("file_path");
        @SuppressWarnings("unchecked")
        List<String> methodNames = (List<String>) request.get("method_names");
        return getService().getMethodModifiers(repoPath, filePath, methodNames);
    }

    /**
     * Fix C: Validate a Java code snippet for syntactic correctness.
     *
     * Request body:
     *   code          — Java code to validate (method body, class member, or full file)
     *   context_class — optional simple class name hint (currently unused, reserved)
     *
     * Response body:
     *   status → "ok"          snippet parsed successfully
     *          → "parse_error" snippet has syntax errors
     *   errors → list of error messages (empty on success)
     */
    @PostMapping("/parse-snippet")
    public Map<String, Object> parseSnippet(@RequestBody Map<String, Object> request) {
        String code = (String) request.getOrDefault("code", "");
        String contextClass = (String) request.getOrDefault("context_class", "");
        return getService().parseSnippet(code, contextClass);
    }

    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "ok", "service", "javaparser");
    }
}
