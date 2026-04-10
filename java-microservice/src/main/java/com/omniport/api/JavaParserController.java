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

    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "ok", "service", "javaparser");
    }
}
