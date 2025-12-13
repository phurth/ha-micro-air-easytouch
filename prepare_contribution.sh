#!/bin/bash
# Script to prepare the EasyTouch integration for contribution
# This automates the steps needed to fork and contribute back to the original repo

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== EasyTouch Integration - Contribution Setup ===${NC}\n"

# Check if we're in a git repository
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    echo -e "${RED}Error: Not in a git repository${NC}"
    exit 1
fi

# Get current branch
CURRENT_BRANCH=$(git branch --show-current)
echo -e "${GREEN}Current branch: ${CURRENT_BRANCH}${NC}"

# Check if we have uncommitted changes
if ! git diff-index --quiet HEAD --; then
    echo -e "${YELLOW}Warning: You have uncommitted changes${NC}"
    echo -e "${YELLOW}Please commit or stash them before proceeding${NC}"
    read -p "Continue anyway? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Step 1: Check current remotes
echo -e "\n${BLUE}Step 1: Checking current remotes...${NC}"
git remote -v

# Step 2: Get fork URL
echo -e "\n${BLUE}Step 2: Setting up remotes${NC}"
echo -e "${YELLOW}Please provide your GitHub username for the fork${NC}"
read -p "GitHub username: " GITHUB_USERNAME

if [ -z "$GITHUB_USERNAME" ]; then
    echo -e "${RED}Error: GitHub username is required${NC}"
    exit 1
fi

FORK_URL="https://github.com/${GITHUB_USERNAME}/ha-micro-air-easytouch.git"
UPSTREAM_URL="https://github.com/k3vmcd/ha-micro-air-easytouch.git"

# Step 3: Configure remotes
echo -e "\n${BLUE}Step 3: Configuring remotes...${NC}"

# Check if origin exists and what it points to
if git remote get-url origin > /dev/null 2>&1; then
    ORIGIN_URL=$(git remote get-url origin)
    if [[ "$ORIGIN_URL" == *"k3vmcd"* ]]; then
        echo -e "${YELLOW}Origin points to original repo, renaming to upstream...${NC}"
        git remote rename origin upstream
    elif [[ "$ORIGIN_URL" == *"${GITHUB_USERNAME}"* ]]; then
        echo -e "${GREEN}Origin already points to your fork${NC}"
    else
        echo -e "${YELLOW}Origin points to: ${ORIGIN_URL}${NC}"
        read -p "Rename origin to upstream and add your fork as origin? (Y/n): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            git remote rename origin upstream
        fi
    fi
fi

# Add upstream if it doesn't exist
if ! git remote get-url upstream > /dev/null 2>&1; then
    echo -e "${GREEN}Adding upstream remote: ${UPSTREAM_URL}${NC}"
    git remote add upstream "$UPSTREAM_URL"
fi

# Add origin (fork) if it doesn't exist
if ! git remote get-url origin > /dev/null 2>&1; then
    echo -e "${GREEN}Adding origin remote (your fork): ${FORK_URL}${NC}"
    git remote add origin "$FORK_URL"
else
    # Update origin URL if it exists
    CURRENT_ORIGIN=$(git remote get-url origin)
    if [[ "$CURRENT_ORIGIN" != "$FORK_URL" ]]; then
        echo -e "${YELLOW}Updating origin URL to: ${FORK_URL}${NC}"
        git remote set-url origin "$FORK_URL"
    fi
fi

# Verify remotes
echo -e "\n${GREEN}Remotes configured:${NC}"
git remote -v

# Step 4: Create feature branch
echo -e "\n${BLUE}Step 4: Creating feature branch...${NC}"
BRANCH_NAME="fix/multiple-adapters-and-concurrent-ops"

# Check if branch already exists
if git show-ref --verify --quiet refs/heads/"$BRANCH_NAME"; then
    echo -e "${YELLOW}Branch ${BRANCH_NAME} already exists${NC}"
    read -p "Switch to it? (Y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        git checkout "$BRANCH_NAME"
    fi
else
    echo -e "${GREEN}Creating branch: ${BRANCH_NAME}${NC}"
    git checkout -b "$BRANCH_NAME"
fi

# Step 5: Check for uncommitted changes
echo -e "\n${BLUE}Step 5: Checking for changes to commit...${NC}"
if ! git diff-index --quiet HEAD --; then
    echo -e "${GREEN}Found uncommitted changes${NC}"
    git status --short
    
    echo -e "\n${YELLOW}Preparing commit message...${NC}"
    COMMIT_MSG="Fix: Serialize BLE operations and handle multiple adapters

- Add per-device asyncio.Lock to prevent concurrent BLE operations
- Store and prefer adapter used during initial setup
- Preserve state on read failures to prevent mode flips
- Optimize BLE operations (combined send+read)
- Fix HVAC mode detection when device is off
- Improve update responsiveness (15s polling + advertisement triggers)
- Fix circular import and YAML validation issues

Fixes #27 (concurrent operations) and adapter switching issues"

    read -p "Commit these changes? (Y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        # Stage all changes
        git add custom_components/micro_air_easytouch/
        git add *.md 2>/dev/null || true  # Documentation files (ignore if none)
        git add custom_components/micro_air_easytouch/services.yaml
        
        # Commit
        git commit -m "$COMMIT_MSG"
        echo -e "${GREEN}Changes committed${NC}"
    else
        echo -e "${YELLOW}Skipping commit${NC}"
    fi
else
    echo -e "${GREEN}No uncommitted changes${NC}"
fi

# Step 6: Push to fork
echo -e "\n${BLUE}Step 6: Pushing to your fork...${NC}"
echo -e "${YELLOW}This will push to: ${FORK_URL}${NC}"
read -p "Push now? (Y/n): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Nn]$ ]]; then
    # Check if branch exists on remote
    if git ls-remote --heads origin "$BRANCH_NAME" | grep -q "$BRANCH_NAME"; then
        echo -e "${YELLOW}Branch exists on remote, will force push if needed${NC}"
        read -p "Force push? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            git push -u origin "$BRANCH_NAME" --force
        else
            git push -u origin "$BRANCH_NAME"
        fi
    else
        git push -u origin "$BRANCH_NAME"
    fi
    echo -e "${GREEN}Branch pushed to your fork${NC}"
else
    echo -e "${YELLOW}Skipping push${NC}"
fi

# Step 7: Summary and next steps
echo -e "\n${GREEN}=== Setup Complete ===${NC}\n"
echo -e "${BLUE}Next steps:${NC}"
echo -e "1. ${YELLOW}If you haven't already, fork the repo on GitHub:${NC}"
echo -e "   ${GREEN}https://github.com/k3vmcd/ha-micro-air-easytouch${NC}"
echo -e "   Click the 'Fork' button in the top right\n"
echo -e "2. ${YELLOW}Create a Pull Request:${NC}"
echo -e "   ${GREEN}https://github.com/${GITHUB_USERNAME}/ha-micro-air-easytouch/compare/main...${BRANCH_NAME}${NC}"
echo -e "   Or go to your fork and click 'New Pull Request'\n"
echo -e "3. ${YELLOW}PR Details:${NC}"
echo -e "   - Title: ${GREEN}Fix: Serialize BLE operations and handle multiple adapters${NC}"
echo -e "   - Description: Reference ${GREEN}CODE_CHANGES_SUMMARY.md${NC} for details"
echo -e "   - Link to issue: ${GREEN}#27${NC}\n"
echo -e "4. ${YELLOW}To keep your fork updated:${NC}"
echo -e "   ${GREEN}git fetch upstream${NC}"
echo -e "   ${GREEN}git rebase upstream/main${NC}"
echo -e "   ${GREEN}git push origin ${BRANCH_NAME}${NC}\n"

echo -e "${GREEN}All done! Your changes are ready for contribution.${NC}"
