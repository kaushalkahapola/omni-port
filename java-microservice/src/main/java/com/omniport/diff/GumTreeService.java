package com.omniport.diff;

import gumtree.spoon.AstComparator;
import gumtree.spoon.diff.Diff;
import gumtree.spoon.diff.operations.Operation;
import spoon.reflect.cu.SourcePosition;

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

            System.setProperty("gumtree.match.bu.sim", "0.2");
            System.setProperty("gumtree.match.bu.size", "600");

            String targetContent = new String(Files.readAllBytes(file.toPath()));

            // ── Step 1: Find the hunk's location in the TARGET FILE via sliding-window ──
            // This is reliable regardless of structural differences.
            int[] location = findLocation(oldContent, targetContent);
            if (location != null) {
                int startLine = location[0];
                int endLine   = location[1];
                String[] allLines = targetContent.split("\n", -1);
                int ctxBefore = Math.max(0, startLine - 1 - 3);
                int ctxAfter  = Math.min(allLines.length, endLine + 3);
                StringBuilder snapshot = new StringBuilder();
                for (int i = ctxBefore; i < ctxAfter; i++) {
                    snapshot.append(allLines[i]).append("\n");
                }
                result.put("start_line", startLine);
                result.put("end_line",   endLine);
                result.put("context_snapshot", snapshot.toString());
                result.put("confidence", 0.90);
            }

            // ── Step 2: Run GumTree for structural diff and symbol mappings ──
            AstComparator comparator = new AstComparator();
            Diff diff = comparator.compare(oldContent, targetContent);

            List<Map<String, String>> operations = new ArrayList<>();
            Map<String, String> symbolMappings = new HashMap<>();

            // Track dst-node positions as a fallback location source.
            int gtMinLine = Integer.MAX_VALUE;
            int gtMaxLine = 0;

            for (Operation op : diff.getRootOperations()) {
                Map<String, String> opMap = new HashMap<>();
                opMap.put("type", op.getAction().getClass().getSimpleName());
                opMap.put("src",  op.getSrcNode() != null ? op.getSrcNode().toString() : "null");
                opMap.put("dst",  op.getDstNode() != null ? op.getDstNode().toString() : "null");
                operations.add(opMap);

                String opType = op.getAction().getClass().getSimpleName();
                if (opType.equals("UpdateOperation") || opType.equals("MoveOperation")) {
                    if (op.getSrcNode() != null && op.getDstNode() != null) {
                        String srcName = op.getSrcNode().getShortRepresentation();
                        String dstName = op.getDstNode().getShortRepresentation();
                        if (srcName != null && dstName != null && !srcName.equals(dstName)) {
                            symbolMappings.put(srcName, dstName);
                        }
                    }
                }

                // Collect dst-node line numbers as fallback if sliding-window failed.
                if (location == null && op.getDstNode() != null) {
                    try {
                        SourcePosition pos = op.getDstNode().getPosition();
                        if (pos != null && pos.isValidPosition()) {
                            int ln = pos.getLine();
                            int le = pos.getEndLine();
                            if (ln > 0 && ln < gtMinLine) gtMinLine = ln;
                            if (le > gtMaxLine) gtMaxLine = le;
                        }
                    } catch (Exception ignored) {}
                }
            }

            // If sliding-window found nothing, try GumTree dst positions.
            if (location == null && gtMaxLine > 0 && gtMinLine != Integer.MAX_VALUE) {
                result.put("start_line", gtMinLine);
                result.put("end_line",   gtMaxLine);
                String[] allLines = targetContent.split("\n", -1);
                int ctxBefore = Math.max(0, gtMinLine - 1 - 3);
                int ctxAfter  = Math.min(allLines.length, gtMaxLine + 3);
                StringBuilder snapshot = new StringBuilder();
                for (int i = ctxBefore; i < ctxAfter; i++) {
                    snapshot.append(allLines[i]).append("\n");
                }
                result.put("context_snapshot", snapshot.toString());
                result.put("confidence", 0.75);
            }

            result.put("status", "ok");
            result.put("operations", operations);
            result.put("symbol_mappings", symbolMappings);

        } catch (Exception e) {
            result.put("status", "error");
            result.put("message", e.getMessage());
        }

        return result;
    }

    /**
     * Sliding-window search: finds where oldContent appears in targetContent
     * using exact line-by-line matching first, then a prefix-trimmed fallback.
     *
     * Returns [startLine, endLine] (1-based) or null if not found.
     */
    private int[] findLocation(String oldContent, String targetContent) {
        String[] oldLines    = oldContent.split("\n", -1);
        String[] targetLines = targetContent.split("\n", -1);

        if (oldLines.length == 0 || oldLines.length > targetLines.length) return null;

        // Remove trailing empty line that splitlines may produce.
        int oldLen = oldLines.length;
        if (oldLen > 0 && oldLines[oldLen - 1].isEmpty()) oldLen--;
        if (oldLen == 0) return null;

        // Pass 1: exact match
        for (int i = 0; i <= targetLines.length - oldLen; i++) {
            boolean match = true;
            for (int j = 0; j < oldLen; j++) {
                if (!oldLines[j].equals(targetLines[i + j])) {
                    match = false;
                    break;
                }
            }
            if (match) return new int[]{i + 1, i + oldLen};
        }

        // Pass 2: trim-based match (handles leading/trailing whitespace drift)
        for (int i = 0; i <= targetLines.length - oldLen; i++) {
            boolean match = true;
            for (int j = 0; j < oldLen; j++) {
                if (!oldLines[j].trim().equals(targetLines[i + j].trim())) {
                    match = false;
                    break;
                }
            }
            if (match) return new int[]{i + 1, i + oldLen};
        }

        return null;
    }
}
