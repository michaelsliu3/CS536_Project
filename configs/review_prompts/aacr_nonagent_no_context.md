You are an expert code reviewer. Your task is to review the following code changes and provide constructive feedback.

## Pull Request Information

**Title:** {{ pr_title }}

**Description:** {{ pr_description }}

## Code Changes to Review

```diff
{{ diff_hunk }}
```

## Instructions

Please review the code changes above and provide detailed feedback. For each issue you identify:

1. Use `<diff>...</diff>` tags to wrap **only the minimal code snippet** that contains the specific issue you're commenting on. Do NOT include the entire diff_hunk - extract only the relevant lines that demonstrate the problem.
2. Use `<side>left</side>` or `<side>right</side>` to indicate whether your comment applies to the old code (left) or new code (right)
3. Use `<note>...</note>` tags to wrap your review comment
4. Use `<notesplit />` to separate different review comments
5. Use `<end />` to end your response

**Important**: The `<diff>` section should contain the smallest possible code fragment that clearly shows the issue. If multiple separate issues exist in different parts of the diff_hunk, create separate review comments for each with their own minimal `<diff>` snippets.

Focus on:
- Code defects
- Performance issues
- Security vulnerability
- Maintainability and readability

## Example Format

<diff>
+ def example_function():
+     pass
</diff>
<side>right</side>
<note>
This function lacks documentation. Please add a docstring explaining its purpose, parameters, and return value.
</note>
<notesplit />
<end />

## Output Requirements

- If you find issues with the code changes, provide your review comments following the format above
- If you believe the code changes are acceptable and have no issues to report, simply output `<end />`
- Always end your response with `<end />` after all review comments (or immediately if no issues found)

Now provide your review:
