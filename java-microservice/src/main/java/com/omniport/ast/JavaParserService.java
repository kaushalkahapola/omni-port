package com.omniport.ast;

import com.github.javaparser.StaticJavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.TypeDeclaration;
import com.github.javaparser.ast.type.ClassOrInterfaceType;
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
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
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
     * Walk the class hierarchy starting from sourceFilePath and locate each method in
     * methodNames, recording the first file where each method is found (searching the
     * class itself first, then its superclasses in BFS order, up to maxDepth levels).
     *
     * Returns a map with:
     *   "status"             → "ok" or "error"
     *   "dominant_file_path" → the relative file path where most methods were found
     *   "method_locations"   → map of methodName → {file_path, start_line, end_line, class_name}
     */
    public Map<String, Object> findMethodDefinitions(
            String repoPath, String sourceFilePath, List<String> methodNames) {

        Map<String, Object> result = new HashMap<>();

        try {
            // Resolve the absolute file path
            File sourceFile = new File(repoPath, sourceFilePath);
            if (!sourceFile.exists()) {
                result.put("status", "error");
                result.put("message", "Source file not found: " + sourceFile.getAbsolutePath());
                return result;
            }

            // Find the src/main/java root by walking up from the source file's directory
            Path srcRoot = findSrcMainJavaRoot(Paths.get(sourceFile.getAbsolutePath()).getParent());

            // BFS over the class hierarchy
            // Queue contains absolute file paths to inspect
            Set<String> visited = new LinkedHashSet<>();
            List<String> queue = new ArrayList<>();
            queue.add(sourceFile.getAbsolutePath());

            Map<String, Map<String, Object>> methodLocations = new HashMap<>();
            Set<String> remaining = new LinkedHashSet<>(methodNames);

            int depth = 0;
            int maxDepth = 5;

            while (!queue.isEmpty() && !remaining.isEmpty() && depth <= maxDepth) {
                List<String> nextQueue = new ArrayList<>();
                for (String absPath : queue) {
                    if (visited.contains(absPath)) continue;
                    visited.add(absPath);

                    File classFile = new File(absPath);
                    if (!classFile.exists()) continue;

                    try {
                        CompilationUnit cu = StaticJavaParser.parse(classFile);

                        // Check for methods of interest in all type declarations
                        for (TypeDeclaration<?> type : cu.getTypes()) {
                            for (MethodDeclaration method : type.getMethods()) {
                                String name = method.getNameAsString();
                                if (remaining.contains(name)) {
                                    int startLine = method.getBegin()
                                            .map(pos -> pos.line).orElse(0);
                                    int endLine = method.getEnd()
                                            .map(pos -> pos.line).orElse(startLine);

                                    // Convert absolute path back to repo-relative path
                                    String relPath = Paths.get(repoPath)
                                            .toAbsolutePath()
                                            .relativize(Paths.get(absPath).toAbsolutePath())
                                            .toString();

                                    Map<String, Object> loc = new HashMap<>();
                                    loc.put("file_path", relPath);
                                    loc.put("start_line", startLine);
                                    loc.put("end_line", endLine);
                                    loc.put("class_name", type.getNameAsString());
                                    methodLocations.put(name, loc);
                                    remaining.remove(name);
                                }
                            }

                            // Enqueue superclass for next BFS level
                            if (type instanceof ClassOrInterfaceDeclaration && srcRoot != null) {
                                ClassOrInterfaceDeclaration classDecl =
                                        (ClassOrInterfaceDeclaration) type;
                                for (ClassOrInterfaceType superType : classDecl.getExtendedTypes()) {
                                    String superName = superType.getNameAsString();
                                    // Resolve FQN via imports
                                    String superFqn = resolveImport(cu, superName);
                                    if (superFqn != null) {
                                        Path superFile = srcRoot.resolve(
                                                superFqn.replace('.', '/') + ".java");
                                        if (Files.exists(superFile)) {
                                            nextQueue.add(superFile.toAbsolutePath().toString());
                                        }
                                    } else {
                                        // Try same package
                                        Optional<String> pkg = cu.getPackageDeclaration()
                                                .map(pd -> pd.getNameAsString());
                                        if (pkg.isPresent() && srcRoot != null) {
                                            Path superFile = srcRoot.resolve(
                                                    pkg.get().replace('.', '/') + "/" + superName + ".java");
                                            if (Files.exists(superFile)) {
                                                nextQueue.add(superFile.toAbsolutePath().toString());
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    } catch (Exception parseEx) {
                        // Skip unparseable files silently
                    }
                }
                queue = nextQueue;
                depth++;
            }

            // Compute dominant_file_path: the relative path with the most method hits
            Map<String, Long> fileCounts = methodLocations.values().stream()
                    .collect(Collectors.groupingBy(
                            loc -> (String) loc.get("file_path"),
                            Collectors.counting()));
            String dominantFile = fileCounts.entrySet().stream()
                    .max(Map.Entry.comparingByValue())
                    .map(Map.Entry::getKey)
                    .orElse(sourceFilePath);

            result.put("status", "ok");
            result.put("dominant_file_path", dominantFile);
            result.put("method_locations", methodLocations);

        } catch (Exception e) {
            result.put("status", "error");
            result.put("message", e.getMessage());
        }

        return result;
    }

    /** Walk up from dir looking for a path component matching src/main/java. */
    private Path findSrcMainJavaRoot(Path dir) {
        Path target = Paths.get("src", "main", "java");
        Path p = dir;
        while (p != null) {
            // Check if the last 3 components match src/main/java
            if (p.getNameCount() >= 3) {
                Path suffix = p.subpath(p.getNameCount() - 3, p.getNameCount());
                if (suffix.equals(target)) {
                    return p;
                }
            }
            p = p.getParent();
        }
        return null;
    }

    /**
     * Resolve a simple class name to its fully-qualified name using the import
     * declarations in the given CompilationUnit. Returns null if not found.
     */
    private String resolveImport(CompilationUnit cu, String simpleName) {
        return cu.getImports().stream()
                .map(imp -> imp.getNameAsString())
                .filter(fqn -> fqn.endsWith("." + simpleName))
                .findFirst()
                .orElse(null);
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
