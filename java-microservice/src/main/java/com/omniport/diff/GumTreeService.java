package com.omniport.diff;

import gumtree.spoon.AstComparator;
import gumtree.spoon.diff.Diff;
import gumtree.spoon.diff.operations.Operation;

import java.io.File;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public class GumTreeService {

    public Map<String, Object> computeDiff(String repoPath, String filePath, String oldContent) {
        Map<String, Object> result = new HashMap<>();

        try {
            File file = new File(repoPath, filePath);
            if (!file.exists()) {
                result.put("status", "error");
                result.put("message", "File not found: " + file.getAbsolutePath());
                return result;
            }

            // Set system properties for GumTree parameters (bu_minsim=0.2, bu_minsize=600)
            System.setProperty("gumtree.match.bu.sim", "0.2");
            System.setProperty("gumtree.match.bu.size", "600");

            String newContent = new String(Files.readAllBytes(file.toPath()));

            AstComparator comparator = new AstComparator();
            Diff diff = comparator.compare(oldContent, newContent);

            List<Map<String, String>> operations = new ArrayList<>();
            Map<String, String> symbolMappings = new HashMap<>();

            for (Operation op : diff.getRootOperations()) {
                Map<String, String> opMap = new HashMap<>();
                opMap.put("type", op.getAction().getClass().getSimpleName());
                opMap.put("src", op.getSrcNode() != null ? op.getSrcNode().toString() : "null");
                opMap.put("dst", op.getDstNode() != null ? op.getDstNode().toString() : "null");
                operations.add(opMap);

                // Try to infer mappings for updates/moves
                if (op.getAction().getClass().getSimpleName().equals("UpdateOperation") ||
                    op.getAction().getClass().getSimpleName().equals("MoveOperation")) {
                    if (op.getSrcNode() != null && op.getDstNode() != null) {
                        String srcName = op.getSrcNode().getShortRepresentation();
                        String dstName = op.getDstNode().getShortRepresentation();
                        if (srcName != null && dstName != null && !srcName.equals(dstName)) {
                            symbolMappings.put(srcName, dstName);
                        }
                    }
                }
            }

            result.put("status", "ok");
            result.put("operations", operations);
            result.put("mappings", symbolMappings);

        } catch (Exception e) {
            result.put("status", "error");
            result.put("message", e.getMessage());
        }

        return result;
    }
}
