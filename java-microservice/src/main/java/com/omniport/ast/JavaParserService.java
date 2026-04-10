package com.omniport.ast;

import com.github.javaparser.StaticJavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.TypeDeclaration;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.CombinedTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.JavaParserTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.JarTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.ReflectionTypeSolver;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;
import java.util.stream.Stream;

public class JavaParserService {

    /**
     * Resolve symbols in a Java file using JavaParser's CombinedTypeSolver.
     *
     * The solver chain:
     *   1. ReflectionTypeSolver      — JDK built-in types (String, List, etc.)
     *   2. JavaParserTypeSolver      — source files under repoPath/src/main/java
     *   3. JarTypeSolver (per JAR)   — compiled dependencies from Maven/Gradle cache
     *
     * For each method whose name appears in symbolsToResolve, we attempt full
     * type-resolution to get the fully-qualified declaration. Falls back to the
     * unresolved declaration string on resolution failure.
     */
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

            // Build CombinedTypeSolver with all available type sources
            CombinedTypeSolver typeSolver = new CombinedTypeSolver();
            typeSolver.add(new ReflectionTypeSolver());

            // Add JavaParser source solver if src/main/java exists
            Path srcMainJava = Paths.get(repoPath, "src", "main", "java");
            if (Files.isDirectory(srcMainJava)) {
                typeSolver.add(new JavaParserTypeSolver(srcMainJava.toFile()));
            }

            // Add JarTypeSolver for each JAR found under target/dependency or .gradle caches
            addJarSolvers(typeSolver, repoPath);

            JavaSymbolSolver symbolSolver = new JavaSymbolSolver(typeSolver);
            StaticJavaParser.getParserConfiguration().setSymbolResolver(symbolSolver);

            CompilationUnit cu = StaticJavaParser.parse(file);

            for (TypeDeclaration<?> type : cu.getTypes()) {
                for (MethodDeclaration method : type.getMethods()) {
                    String methodName = method.getNameAsString();
                    if (!symbolsToResolve.contains(methodName)) {
                        continue;
                    }

                    // Attempt full symbol resolution for qualified type info
                    String declaration;
                    try {
                        var resolved = method.resolve();
                        // Build a qualified declaration: ReturnType ClassName.methodName(ParamTypes)
                        String returnType = resolved.getReturnType().describe();
                        String qualifiedName = resolved.getQualifiedName();
                        String params = resolved.getNumberOfParams() == 0 ? "" :
                            java.util.stream.IntStream.range(0, resolved.getNumberOfParams())
                                .mapToObj(i -> resolved.getParam(i).describeType())
                                .collect(Collectors.joining(", "));
                        declaration = returnType + " " + qualifiedName + "(" + params + ")";
                    } catch (Exception resolveEx) {
                        // Fall back to unresolved declaration (still better than just the name)
                        declaration = method.getDeclarationAsString(false, false, true);
                    }

                    resolvedMappings.put(methodName, declaration);
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

    /**
     * Add JarTypeSolver instances for all JARs found in common dependency cache paths
     * under repoPath (Maven target/dependency, Gradle caches).
     */
    private void addJarSolvers(CombinedTypeSolver typeSolver, String repoPath) {
        // Maven: target/dependency JARs
        Path mavenDeps = Paths.get(repoPath, "target", "dependency");
        addJarsFromDirectory(typeSolver, mavenDeps);

        // Gradle: .gradle/caches (walk for *.jar, depth-limited to avoid scanning everything)
        Path gradleCaches = Paths.get(repoPath, ".gradle", "caches");
        addJarsFromDirectory(typeSolver, gradleCaches);

        // Also check for local Maven repo under ~/.m2/repository if available
        Path m2Repo = Paths.get(System.getProperty("user.home"), ".m2", "repository");
        // Only add if repoPath uses Maven (pom.xml exists) — too expensive to scan all of .m2
        if (Files.exists(Paths.get(repoPath, "pom.xml")) && Files.isDirectory(m2Repo)) {
            // Don't add all of .m2 — too many JARs. Only add what's in target/dependency.
            // This is already covered above.
        }
    }

    private void addJarsFromDirectory(CombinedTypeSolver typeSolver, Path dir) {
        if (!Files.isDirectory(dir)) return;
        try (Stream<Path> stream = Files.walk(dir, 5)) {
            stream.filter(p -> p.toString().endsWith(".jar"))
                  .forEach(jarPath -> {
                      try {
                          typeSolver.add(new JarTypeSolver(jarPath.toFile()));
                      } catch (IOException ignored) {
                          // Skip JARs that can't be read (corrupted, write-locked, etc.)
                      }
                  });
        } catch (IOException ignored) {
            // Directory not accessible — skip silently
        }
    }
}
