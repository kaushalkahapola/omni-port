package com.omniport;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.util.HashMap;
import java.util.Map;

public class OmniPortServer {

    private static final ObjectMapper mapper = new ObjectMapper();

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
        
        switch (command) {
            case "ping":
                response.put("status", "ok");
                response.put("message", "pong");
                break;
            case "javaparser_resolve":
                // TODO: Implement JavaParser logic
                response.put("status", "ok");
                response.put("message", "javaparser resolution not yet implemented");
                break;
            case "gumtree_diff":
                // TODO: Implement GumTree logic
                response.put("status", "ok");
                response.put("message", "gumtree diffing not yet implemented");
                break;
            case "japicmp_compare":
                // TODO: Implement japicmp logic
                response.put("status", "ok");
                response.put("message", "japicmp comparison not yet implemented");
                break;
            default:
                response.put("status", "error");
                response.put("message", "Unknown command: " + command);
        }
        
        return response;
    }
}