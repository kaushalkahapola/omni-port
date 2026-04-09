package com.omniport.api;

import japicmp.cmp.JApiCmpArchive;
import japicmp.cmp.JarArchiveComparator;
import japicmp.cmp.JarArchiveComparatorOptions;
import japicmp.config.Options;
import japicmp.model.JApiClass;

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

            List<String> modifiedClasses = new ArrayList<>();
            for (JApiClass jApiClass : jApiClasses) {
                if (jApiClass.getChangeStatus().name().equals("MODIFIED") || 
                    jApiClass.getChangeStatus().name().equals("NEW") || 
                    jApiClass.getChangeStatus().name().equals("REMOVED")) {
                    modifiedClasses.add(jApiClass.getFullyQualifiedName() + " (" + jApiClass.getChangeStatus().name() + ")");
                }
            }

            result.put("status", "ok");
            result.put("modified_classes", modifiedClasses);

        } catch (Exception e) {
            result.put("status", "error");
            result.put("message", e.getMessage());
        }

        return result;
    }
}
