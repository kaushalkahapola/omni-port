package com.omniport;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.omniport.ast.JavaParserService;
import com.omniport.diff.GumTreeService;
import com.omniport.api.JapicmpService;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public class OmniPortServer {

    private static final ObjectMapper mapper = new ObjectMapper();
    private static final JavaParserService javaParserService = new JavaParserService();
    private static final GumTreeService gumTreeService = new GumTreeService();
    private static final JapicmpService japicmpService = new JapicmpService();

    public static void main(String[] args) {
        System.out.println("OmniPort Java Microservice started. Awaiting JSON commands on stdin...");
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(System.in))) {
            String line;
            while ((line = reader.readLine()) != null) {
                if (line.trim().isEmpty()) continue;
                if (line.trim().equalsIgnoreCase("exit")) break;

                try {
                    Map<String, Object> request = mapper.readValue(line, Map.class);
                    Map<String, Object> response = handleRequest(request);
                    System.out.println(mapper.writeValueAsString(response));
                } catch (Exception e) {
                    Map<String, Object> errorResponse = new HashMap<>();
                    errorResponse.put("status", "error");
                    errorResponse.put("message", e.getMessage());
                    System.out.println(mapper.writeValueAsString(errorResponse));
                }
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
        System.out.println("OmniPort Java Microservice shutting down.");
    }

    private static Map<String, Object> handleRequest(Map<String, Object> request) {
        String command = (String) request.getOrDefault("command", "ping");
        Map<String, Object> response = new HashMap<>();
        
        try {
            switch (command) {
                case "ping":
                    response.put("status", "ok");
                    response.put("message", "pong");
                    break;
                case "javaparser_resolve":
                    String jpRepo = (String) request.get("repo_path");
                    String jpPath = (String) request.get("file_path");
                    List<String> symbols = (List<String>) request.get("symbols_to_resolve");
                    response = javaParserService.resolveSymbols(jpRepo, jpPath, symbols);
                    break;
                case "gumtree_diff":
                    String gtRepo = (String) request.get("repo_path");
                    String gtPath = (String) request.get("file_path");
                    String gtOldContent = (String) request.get("old_content");
                    response = gumTreeService.computeDiff(gtRepo, gtPath, gtOldContent);
                    break;
                case "japicmp_compare":
                    String oldJar = (String) request.get("old_jar_path");
                    String newJar = (String) request.get("new_jar_path");
                    response = japicmpService.compareJars(oldJar, newJar);
                    break;
                default:
                    response.put("status", "error");
                    response.put("message", "Unknown command: " + command);
            }
        } catch (Exception e) {
            response.put("status", "error");
            response.put("message", "Exception executing command " + command + ": " + e.getMessage());
        }
        
        return response;
    }
}
