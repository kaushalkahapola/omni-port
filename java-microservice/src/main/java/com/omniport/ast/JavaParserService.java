package com.omniport.ast;

import com.github.javaparser.StaticJavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.NodeList;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.TypeDeclaration;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.CombinedTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.ReflectionTypeSolver;

import java.io.File;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public class JavaParserService {

    private CombinedTypeSolver typeSolver;
    private JavaSymbolSolver symbolSolver;

    public JavaParserService() {
        typeSolver = new CombinedTypeSolver();
        typeSolver.add(new ReflectionTypeSolver());
        symbolSolver = new JavaSymbolSolver(typeSolver);
        StaticJavaParser.getParserConfiguration().setSymbolResolver(symbolSolver);
    }

    public Map<String, Object> resolveSymbols(String repoPath, String filePath, List<String> symbolsToResolve) {
        Map<String, Object> result = new HashMap<>();
        Map<String, String> resolvedMappings = new HashMap<>();

        try {
            File file = new File(repoPath, filePath);
            if (!file.exists()) {
                result.put("status", "error");
                result.put("message", "File not found: " + file.getAbsolutePath());
                return result;
            }

            CompilationUnit cu = StaticJavaParser.parse(file);
            
            // Simple mapping for now: extract method signatures to find potential matches
            for (TypeDeclaration<?> type : cu.getTypes()) {
                for (MethodDeclaration method : type.getMethods()) {
                    String methodName = method.getNameAsString();
                    if (symbolsToResolve.contains(methodName)) {
                        resolvedMappings.put(methodName, method.getDeclarationAsString(false, false, false));
                    }
                }
            }

            result.put("status", "ok");
            result.put("mappings", resolvedMappings);

        } catch (Exception e) {
            result.put("status", "error");
            result.put("message", e.getMessage());
        }

        return result;
    }
}
