package com.omniport.api;

import japicmp.cmp.JApiCmpArchive;
import japicmp.cmp.JarArchiveComparator;
import japicmp.cmp.JarArchiveComparatorOptions;
import japicmp.config.Options;
import japicmp.model.JApiClass;
import japicmp.model.JApiCompatibilityChange;
import japicmp.model.JApiField;
import japicmp.model.JApiMethod;

import java.io.File;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public class JapicmpService {

    public Map<String, Object> compareJars(String oldJarPath, String newJarPath) {
        Map<String, Object> result = new HashMap<>();

        try {
            File oldJar = new File(oldJarPath);
            File newJar = new File(newJarPath);

            if (!oldJar.exists() || !newJar.exists()) {
                result.put("status", "error");
                result.put("message", "One or both JARs not found");
                return result;
            }

            Options options = Options.newDefault();
            JarArchiveComparatorOptions comparatorOptions = JarArchiveComparatorOptions.of(options);
            JarArchiveComparator comparator = new JarArchiveComparator(comparatorOptions);

            JApiCmpArchive oldArchive = new JApiCmpArchive(oldJar, "old");
            JApiCmpArchive newArchive = new JApiCmpArchive(newJar, "new");

            List<JApiClass> jApiClasses = comparator.compare(oldArchive, newArchive);

            // Top-level summary: changed classes
            List<String> modifiedClasses = new ArrayList<>();
            // Detailed member-level changes: list of {class, member, kind, change_status}
            List<Map<String, String>> memberChanges = new ArrayList<>();

            for (JApiClass jApiClass : jApiClasses) {
                String classStatus = jApiClass.getChangeStatus().name();
                if (!classStatus.equals("UNCHANGED")) {
                    modifiedClasses.add(jApiClass.getFullyQualifiedName() + " (" + classStatus + ")");
                }

                // Method-level changes
                for (JApiMethod method : jApiClass.getMethods()) {
                    String methodStatus = method.getChangeStatus().name();
                    if (!methodStatus.equals("UNCHANGED")) {
                        Map<String, String> entry = new HashMap<>();
                        entry.put("class", jApiClass.getFullyQualifiedName());
                        entry.put("member", method.getName() + buildParamDesc(method));
                        entry.put("kind", "method");
                        entry.put("change_status", methodStatus);
                        entry.put("binary_compatible", String.valueOf(method.isBinaryCompatible()));

                        // Collect compatibility change descriptions (JApiCompatibilityChange is an interface)
                        List<String> changes = new ArrayList<>();
                        for (JApiCompatibilityChange cc : method.getCompatibilityChanges()) {
                            changes.add(cc.toString());
                        }
                        if (!changes.isEmpty()) {
                            entry.put("compatibility_changes", String.join(", ", changes));
                        }

                        memberChanges.add(entry);
                    }
                }

                // Field-level changes
                for (JApiField field : jApiClass.getFields()) {
                    String fieldStatus = field.getChangeStatus().name();
                    if (!fieldStatus.equals("UNCHANGED")) {
                        Map<String, String> entry = new HashMap<>();
                        entry.put("class", jApiClass.getFullyQualifiedName());
                        entry.put("member", field.getName());
                        entry.put("kind", "field");
                        entry.put("change_status", fieldStatus);
                        entry.put("binary_compatible", String.valueOf(field.isBinaryCompatible()));

                        List<String> changes = new ArrayList<>();
                        for (JApiCompatibilityChange cc : field.getCompatibilityChanges()) {
                            changes.add(cc.toString());
                        }
                        if (!changes.isEmpty()) {
                            entry.put("compatibility_changes", String.join(", ", changes));
                        }

                        memberChanges.add(entry);
                    }
                }
            }

            result.put("status", "ok");
            result.put("modified_classes", modifiedClasses);
            result.put("member_changes", memberChanges);

        } catch (Exception e) {
            result.put("status", "error");
            result.put("message", e.getMessage());
        }

        return result;
    }

    /**
     * Build a parameter descriptor string for a method, e.g., "(String, int)".
     * Uses Javassist's CtClass array from getNewMethod() / getOldMethod().
     */
    private String buildParamDesc(JApiMethod method) {
        try {
            javassist.CtClass[] params = null;
            if (method.getNewMethod().isPresent()) {
                params = method.getNewMethod().get().getParameterTypes();
            } else if (method.getOldMethod().isPresent()) {
                params = method.getOldMethod().get().getParameterTypes();
            }
            if (params == null || params.length == 0) return "()";
            StringBuilder sb = new StringBuilder("(");
            for (int i = 0; i < params.length; i++) {
                if (i > 0) sb.append(", ");
                sb.append(params[i].getSimpleName());
            }
            sb.append(")");
            return sb.toString();
        } catch (Exception e) {
            return "()";
        }
    }
}
